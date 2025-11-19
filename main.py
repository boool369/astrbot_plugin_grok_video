import asyncio
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import httpx
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    # 尝试导入 NapCat 文件转发模块
    from .utils.file_send_server import send_file
except ImportError:
    plugin_dir = Path(__file__).parent
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.append(plugin_dir_str)
    try:
        from utils.file_send_server import send_file  # type: ignore
    except ImportError:
        send_file = None
        logger.warning("NapCat 文件转发模块未找到，将跳过 NapCat 中转功能")


@register("grok-video", "Claude", "Grok视频生成插件，支持根据图片和提示词生成视频", "1.0.0")
class GrokVideoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # API配置
        self.server_url = config.get("server_url", "https://api.x.ai").rstrip('/')
        self.model_id = config.get("model_id", "grok-imagine-0.9")
        self.api_key = config.get("api_key", "")
        self.enabled = config.get("enabled", True)
        
        # 请求配置
        self.timeout_seconds = config.get("timeout_seconds", 180)
        self.max_retry_attempts = config.get("max_retry_attempts", 3)
        
        # 群组控制
        self.group_control_mode = config.get("group_control_mode", "off").lower()
        self.group_list = list(config.get("group_list", []))
        
        # 速率限制
        self.rate_limit_enabled = config.get("rate_limit_enabled", True)
        self.rate_limit_window_seconds = config.get("rate_limit_window_seconds", 3600)
        self.rate_limit_max_calls = config.get("rate_limit_max_calls", 5)
        self._rate_limit_bucket = {}  # group_id -> {"window_start": float, "count": int}
        self._rate_limit_locks = {}  # group_id -> asyncio.Lock() 用于并发安全
        self._processing_tasks = {}  # user_id -> task_id 防止重复触发
        
        # 管理员用户（优化为set提高查询效率）
        self.admin_users = set(str(u) for u in config.get("admin_users", []))

        # NapCat 配置，用于文件系统路径问题
        self.nap_server_address = (config.get("nap_server_address") or "").strip()
        nap_port = config.get("nap_server_port")
        try:
            self.nap_server_port = int(nap_port)
        except (TypeError, ValueError):
            self.nap_server_port = 0

        # 强制启用视频保存，因为要使用 fromFileSystem
        self.save_video_enabled = True # config.get("save_video_enabled", False)

        # 使用 AstrBot data 目录保存视频，确保 NapCat 可访问
        try:
            plugin_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_grok_video"))
            self.videos_dir = plugin_data_dir / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.videos_dir = self.videos_dir.resolve()
        except Exception as e:
            # 如果StarTools不可用，使用插件目录下的videos文件夹
            logger.warning(f"无法使用StarTools数据目录，使用插件目录: {e}")
            self.videos_dir = Path(__file__).parent / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.videos_dir = self.videos_dir.resolve()
        
        # 构建完整的API URL
        self.api_url = urljoin(self.server_url + "/", "v1/chat/completions")
        
        logger.info(f"Grok视频生成插件已初始化，API地址: {self.api_url}")
    
    # --- 辅助函数 (保持不变) ---

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        return str(event.get_sender_id()) in self.admin_users

    def _get_callback_api_base(self) -> Optional[str]:
        """读取 AstrBot 全局 callback_api_base 配置"""
        try:
            config = self.context.get_config()
            if isinstance(config, dict):
                return config.get("callback_api_base")
        except Exception as e:
            logger.debug(f"读取 callback_api_base 失败: {e}")
        return None

    async def _check_group_access(self, event: AstrMessageEvent) -> Optional[str]:
        """检查群组访问权限和速率限制（并发安全）"""
        try:
            group_id = None
            try:
                group_id = event.get_group_id()
            except Exception:
                group_id = None

            # 群组白名单/黑名单检查
            if group_id:
                if self.group_control_mode == "whitelist" and group_id not in self.group_list:
                    return "当前群组未被授权使用视频生成功能"
                if self.group_control_mode == "blacklist" and group_id in self.group_list:
                    return "当前群组已被限制使用视频生成功能"

                # 速率限制检查（仅对群组）- 使用异步锁确保并发安全
                if self.rate_limit_enabled:
                    if group_id not in self._rate_limit_locks:
                        self._rate_limit_locks[group_id] = asyncio.Lock()
                    
                    async with self._rate_limit_locks[group_id]:
                        now = time.time()
                        bucket = self._rate_limit_bucket.get(group_id, {"window_start": now, "count": 0})
                        window_start = bucket.get("window_start", now)
                        count = int(bucket.get("count", 0))
                        
                        if now - window_start >= self.rate_limit_window_seconds:
                            window_start = now
                            count = 0
                        
                        if count >= self.rate_limit_max_calls:
                            return f"本群调用已达上限（{self.rate_limit_max_calls}次/{self.rate_limit_window_seconds}秒），请稍后再试"
                        
                        bucket["window_start"], bucket["count"] = window_start, count + 1
                        self._rate_limit_bucket[group_id] = bucket

        except Exception as e:
            logger.error(f"群组访问检查失败: {e}")
            return None
        
        return None

    async def _extract_images_from_message(self, event: AstrMessageEvent) -> List[str]:
        """从消息中提取图片的base64数据"""
        images = []
        
        if hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    try:
                        base64_data = await comp.convert_to_base64()
                        if base64_data:
                            if not base64_data.startswith('data:'):
                                base64_data = f"data:image/jpeg;base64,{base64_data}"
                            images.append(base64_data)
                    except Exception as e:
                        logger.warning(f"图片转base64失败: {e}")
                elif isinstance(comp, Reply) and comp.chain:
                    for reply_comp in comp.chain:
                        if isinstance(reply_comp, Image):
                            try:
                                base64_data = await reply_comp.convert_to_base64()
                                if base64_data:
                                    if not base64_data.startswith('data:'):
                                        base64_data = f"data:image/jpeg;base64,{base64_data}"
                                    images.append(base64_data)
                            except Exception as e:
                                logger.warning(f"引用图片转base64失败: {e}")
        
        return images

    async def _call_grok_api(self, prompt: str, image_base64: str) -> Tuple[Optional[str], Optional[str]]:
        """调用Grok API生成视频"""
        if not self.api_key:
            return None, "未配置API密钥"
        
        # 构建请求数据
        payload = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_base64
                            }
                        }
                    ]
                }
            ]
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        timeout_config = httpx.Timeout(
            connect=10.0,
            read=self.timeout_seconds,
            write=10.0,
            pool=self.timeout_seconds + 10
        )
        
        for attempt in range(self.max_retry_attempts):
            try:
                logger.info(f"调用Grok API (尝试 {attempt + 1}/{self.max_retry_attempts})")
                
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    response = await client.post(
                        self.api_url,
                        json=payload,
                        headers=headers
                    )
                    
                    logger.info(f"API响应状态码: {response.status_code}")
                    response_text = response.text
                    
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            
                            video_url, parse_error = self._extract_video_url_from_response(result)
                            if parse_error:
                                return None, parse_error
                            
                            if video_url:
                                logger.info(f"成功提取到视频URL: {video_url}")
                                return video_url, None
                            else:
                                return None, "API响应中未包含有效的视频URL"
                        except json.JSONDecodeError as e:
                            return None, f"API响应JSON解析失败: {str(e)}, 响应内容: {response_text[:200]}"
                    
                    # ... (省略错误处理逻辑，与之前保持一致) ...
                    elif response.status_code == 403:
                        return None, "API访问被拒绝，请检查密钥和权限"
                    
                    else:
                        error_msg = f"API请求失败 (状态码: {response.status_code})"
                        try:
                            error_detail = response.json()
                            if "error" in error_detail:
                                error_msg += f": {error_detail['error']}"
                            elif "message" in error_detail:
                                error_msg += f": {error_detail['message']}"
                            else:
                                error_msg += f": {error_detail}"
                        except:
                            error_msg += f": {response_text[:200]}"
                        
                        if attempt == self.max_retry_attempts - 1:
                            return None, error_msg
                        
                        logger.warning(f"{error_msg}，等待重试...")
                        await asyncio.sleep(2)
            
            except httpx.TimeoutException:
                error_msg = f"请求超时 ({self.timeout_seconds}秒)"
                if attempt == self.max_retry_attempts - 1:
                    return None, error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)
            
            except Exception as e:
                error_msg = f"请求异常: {str(e)}"
                if attempt == self.max_retry_attempts - 1:
                    return None, error_msg
                logger.warning(f"{error_msg}，等待重试...")
                await asyncio.sleep(1)
        
        return None, "所有重试均失败"

    # --- 视频 URL 提取和处理逻辑 (与上一次修复保持一致) ---

    def _resolve_url(self, url: Optional[str]) -> Optional[str]:
        """将提取到的相对路径 URL 解析为完整的绝对路径，如果是绝对路径则直接返回"""
        if not url:
            return None
            
        # 如果是相对路径，使用 self.server_url 拼接
        if url.startswith("/"):
            resolved_url = urljoin(self.server_url + "/", url.lstrip("/"))
            logger.info(f"相对路径已解析为: {resolved_url}")
            url = resolved_url
            
        # 验证 URL 是否有效（现在应该是绝对路径）
        if not self._is_valid_video_url(url):
            return None
            
        return url

    def _extract_video_url_from_response(self, response_data: dict) -> Tuple[Optional[str], Optional[str]]:
        """从 API 响应中提取视频 URL，并处理相对路径。"""
        try:
            # ... (提取逻辑不变) ...
            if not isinstance(response_data, dict):
                return None, f"无效的响应格式: {type(response_data)}"
            if "choices" not in response_data or not response_data["choices"]:
                return None, "API响应中缺少 choices 字段"
            choice = response_data["choices"][0]
            if not isinstance(choice, dict) or "message" not in choice:
                return None, "choices[0] 缺少 message 字段"
            message = choice["message"]
            if not isinstance(message, dict) or "content" not in message:
                return None, "message 缺少 content 字段"
            content = message["content"]
            if not isinstance(content, str):
                return None, f"content 不是字符串类型: {type(content)}"

            # 3. 优先尝试结构化解析
            video_url = self._try_structured_extraction(response_data)
            resolved_url = self._resolve_url(video_url)
            if resolved_url:
                return resolved_url, None
            
            # 4. 如果结构化解析失败，使用改进的文本解析
            video_url = self._try_content_extraction(content)
            resolved_url = self._resolve_url(video_url)
            if resolved_url:
                return resolved_url, None
            
            # 5. 所有方法都失败
            logger.warning(f"无法从响应中提取视频URL，内容片段: {content[:200]}...")
            return None, f"未能从 API 响应中提取到有效的视频 URL"
            
        except Exception as e:
            logger.error(f"URL 提取过程中发生异常: {e}")
            return None, f"URL 提取失败: {str(e)}"

    def _try_structured_extraction(self, response_data: dict) -> Optional[str]:
        # ... (与上一次修复保持一致) ...
        try:
            if "video_url" in response_data:
                url = response_data["video_url"]
                if isinstance(url, str):
                    logger.info("使用结构化 video_url 字段")
                    return url
            
            choice = response_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            
            for field in ["attachments", "media", "files"]:
                if field in message and isinstance(message[field], list):
                    for item in message[field]:
                        if isinstance(item, dict) and "url" in item:
                            url = item["url"]
                            if isinstance(url, str) and url.lower().endswith(".mp4"):
                                logger.info(f"使用结构化 {field} 字段")
                                return url
            
            return None
            
        except Exception as e:
            logger.debug(f"结构化提取失败: {e}")
            return None
    
    def _try_content_extraction(self, content: str) -> Optional[str]:
        # ... (与上一次修复保持一致) ...
        try:
            video_url = self._extract_from_html_tag(content)
            if video_url: return video_url
            video_url = self._extract_direct_url(content)
            if video_url: return video_url
            video_url = self._extract_from_markdown(content)
            if video_url: return video_url
            return None
        except Exception as e:
            logger.debug(f"内容提取失败: {e}")
            return None

    def _extract_from_html_tag(self, content: str) -> Optional[str]:
        # ... (与上一次修复保持一致) ...
        if "<video" not in content or "src=" not in content:
            return None
        patterns = [
            r'<video[^>]*src=["\']([^"\'>]+)["\'][^>]*>',
            r'src=["\']([^"\'>]+\.mp4[^"\'>]*)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1)
                logger.debug(f"从 HTML 标签提取到 URL: {url}")
                return url
        return None

    def _extract_direct_url(self, content: str) -> Optional[str]:
        # ... (与上一次修复保持一致) ...
        pattern = r'((?:https?://|/)[^\s<>"\')\]\}]+\.mp4(?:\?[^\s<>"\')\]\}]*)?)'
        matches = re.findall(pattern, content, re.IGNORECASE)
        for url in matches:
            logger.debug(f"提取到直接 URL: {url}")
            return url
        return None
    
    def _extract_from_markdown(self, content: str) -> Optional[str]:
        # ... (与上一次修复保持一致) ...
        patterns = [
            r'!?\[[^\]]*\]\(([^\)]+\.mp4[^\)]*)\)',
            r'!?\[[^\]]*\]:\s*([^\s]+\.mp4[^\s]*)',
        ]
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1)
                logger.debug(f"从 Markdown 提取到 URL: {url}")
                return url
        return None
    
    def _is_valid_video_url(self, url: str) -> bool:
        # ... (与上一次修复保持一致) ...
        if not isinstance(url, str) or len(url) < 10:
            return False
        if not url.startswith(("http://", "https://")):
            return False
        if not url.lower().endswith(".mp4") and ".mp4" not in url.lower():
            return False
        invalid_chars = ['<', '>', '"', "'", '\n', '\r', '\t']
        if any(char in url for char in invalid_chars):
            return False
        return True

    # --- 视频下载和发送逻辑 (主要修改区域) ---

    async def _download_video(self, video_url: str) -> Optional[str]:
        """下载视频到本地"""
        try:
            filename = f"grok_video_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.mp4"
            file_path = self.videos_dir / filename
            
            timeout_config = httpx.Timeout(
                connect=10.0,
                read=300.0,
                write=10.0,
                pool=300.0
            )
            
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.get(video_url)
                response.raise_for_status()
                
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                absolute_path = file_path.resolve()
                logger.info(f"视频已保存到: {absolute_path}")
                return str(absolute_path)
            
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            return None

    async def _prepare_video_path(self, video_path: str) -> str:
        """
        [修改点] 强制文件发送模式下，即使使用 NapCat，也只返回本地路径。
        如果需要 NapCat 帮助文件系统可见性，NapCat 必须返回一个底层协议端可识别的**路径**，而不是 URL。
        """
        if not video_path:
            return video_path
        if not (self.nap_server_address and self.nap_server_port):
            return video_path
        if send_file is None:
            logger.debug("NapCat 文件转发模块不可用，直接返回本地路径")
            return video_path
        
        try:
            # 调用 NapCat，但我们仍然希望最终使用 fromFileSystem
            # NapCat 此时的作用是确保文件对协议端可见，它可能返回一个临时的本地路径
            # 或者一个需要通过 fromURL 发送的链接。
            # 鉴于用户要求强制 fromFileSystem，我们只在 NapCat 失败时打印警告，
            # 并且继续使用原始的本地路径，这要求协议端必须能访问这个路径。
            forwarded_path = await send_file(video_path, self.nap_server_address, self.nap_server_port)
            
            # 如果 NapCat 返回的不是 URL，我们使用它。如果返回 URL，我们忽略它并使用原始路径。
            if forwarded_path and not forwarded_path.startswith(("http://", "https://")):
                logger.info(f"NapCat file server 返回了本地路径/标识: {forwarded_path}，使用它")
                return forwarded_path
            
            logger.warning("NapCat 返回了 URL 或无效路径，为遵守 fromFileSystem 要求，将使用原始本地路径。")
        except Exception as e:
            logger.warning(f"NapCat 文件转发失败，将使用原始本地路径: {e}")
            
        # 无论 NapCat 成功与否，都返回本地路径，强制使用 fromFileSystem
        return video_path

    async def _cleanup_video_file(self, video_path: Optional[str]):
        """删除临时视频缓存（按照配置可选）"""
        if not video_path:
            return
        if not self.save_video_enabled: # 始终清理，因为我们强制下载
            return
        try:
            path = Path(video_path)
            if path.exists():
                path.unlink()
                logger.debug(f"已清理本地视频缓存: {path}")
        except Exception as e:
            logger.warning(f"清理视频文件失败: {e}")

    async def _create_video_component(self, video_path: Optional[str], video_url: Optional[str]):
        """
        [修改点] 强制使用 Video.fromFileSystem。
        注意：这在 Docker 部署中极易因文件路径不一致而失败！
        """
        from astrbot.api.message_components import Video

        if not video_path:
            # 理论上 save_video_enabled=True 确保 video_path 存在
            raise ValueError("本地视频路径缺失，无法使用 fromFileSystem 发送")
        
        # 强制使用 fromFileSystem
        logger.warning(f"⚠️ 强制使用 Video.fromFileSystem 发送本地文件: {video_path}")
        logger.warning("⚠️ 此方法要求 AstrBot 与协议端处于同一文件系统或使用 NapCat 传递了正确的本地路径标识。")
        
        return Video.fromFileSystem(path=video_path)

    async def _generate_video_core(self, event: AstrMessageEvent, prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """核心视频生成逻辑"""
        if not self.enabled:
            return None, None, "视频生成功能已禁用"
        
        images = await self._extract_images_from_message(event)
        if not images:
            return None, None, "未找到图片，请在消息中包含图片或引用包含图片的消息"
        
        image_base64 = images[0]
        
        video_url, error_msg = await self._call_grok_api(prompt, image_base64)
        if error_msg:
            return None, None, error_msg

        if not video_url:
            return None, None, "API未返回视频URL"

        # 强制下载视频到本地，因为要使用 fromFileSystem
        local_path = await self._download_video(video_url)
        if not local_path:
             return None, None, "视频下载到本地失败，无法使用 fromFileSystem 发送"

        # local_path 包含了视频的绝对路径
        return video_url, local_path, None

    async def _async_generate_video(self, event: AstrMessageEvent, prompt: str, task_id: str):
        """异步视频生成，避免超时和重复触发"""
        user_id = str(event.get_sender_id())
        video_path = None # 初始化
        try:
            logger.info(f"开始处理用户 {user_id} 的视频生成任务: {task_id}")
            
            video_url, video_path, error_msg = await self._generate_video_core(event, prompt)
            
            if error_msg:
                await event.send(event.plain_result(f"❌ {error_msg}"))
                return
            
            if video_path:
                try:
                    await event.send(event.plain_result("📤 正在发送视频文件..."))
                    
                    # 准备发送路径（此处会处理 NapCat 转发但只返回本地路径）
                    # 这是关键一步，确保了协议端拿到的是它应该能识别的路径/标识
                    final_send_path = await self._prepare_video_path(video_path) 
                    
                    video_component = await self._create_video_component(final_send_path, video_url)
                    
                    try:
                        await asyncio.wait_for(
                            event.send(event.chain_result([video_component])),
                            timeout=90.0
                        )
                        logger.info(f"用户 {user_id} 的视频文件发送成功")
                        await event.send(event.plain_result("✅ 视频文件发送成功！"))
                        
                    except asyncio.TimeoutError:
                        logger.warning(f"用户 {user_id} 的视频发送超时，但可能仍在传输")
                        await event.send(event.plain_result(
                            "⚠️ 视频发送超时，但可能仍在传输中。"
                        ))
                    
                except Exception as e:
                    if "WebSocket API call timeout" in str(e):
                        logger.warning(f"用户 {user_id} 的视频发送WebSocket超时: {e}")
                        await event.send(event.plain_result(
                            "⚠️ 视频发送超时，但可能仍在传输中。"
                        ))
                    else:
                        logger.error(f"用户 {user_id} 的视频文件发送失败: {e}")
                        await event.send(event.plain_result(f"❌ 视频文件发送失败: {str(e)}"))
            else:
                await event.send(event.plain_result("❌ 视频生成失败，请稍后再试"))
        
        except Exception as e:
            logger.error(f"用户 {user_id} 的异步视频生成异常: {e}")
            await event.send(event.plain_result(f"❌ 视频生成时遇到问题: {str(e)}"))
        
        finally:
            # 清理文件
            await self._cleanup_video_file(video_path)
            
            # 清理任务记录
            if user_id in self._processing_tasks and self._processing_tasks[user_id] == task_id:
                del self._processing_tasks[user_id]
                logger.info(f"用户 {user_id} 的任务 {task_id} 已完成")

    # --- 命令函数 (保持不变) ---

    @filter.command("视频")
    async def cmd_generate_video(self, event: AstrMessageEvent, *, prompt: str):
        """生成视频：/视频 <提示词>（需要包含图片）"""
        access_error = await self._check_group_access(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        
        user_id = str(event.get_sender_id())
        if user_id in self._processing_tasks:
            yield event.plain_result(f"⚠️ 您已有一个视频生成任务在进行中，请等待完成后再试。")
            return
        
        images = await self._extract_images_from_message(event)
        if not images:
            yield event.plain_result("❌ 视频生成需要您在消息中包含图片。请上传图片后再试。")
            return
        
        try:
            import uuid
            task_id = str(uuid.uuid4())[:8]
            self._processing_tasks[user_id] = task_id
            
            yield event.plain_result(
                f"🎥 正在使用Grok为您生成视频，请稍候（预计需要几分钟）...\n"
                f"🆔 任务ID: {task_id}\n"
                "📝 提示：本次使用本地文件发送，如果发送失败，请检查Bot与协议端的文件路径配置。"
            )
            
            asyncio.create_task(self._async_generate_video(event, prompt, task_id))
        
        except Exception as e:
            logger.error(f"视频生成命令异常: {e}")
            yield event.plain_result(f"❌ 生成视频时遇到问题: {str(e)}")

    @filter.command("grok测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试Grok API连接（管理员专用）"""
        if not self._is_admin(event):
            yield event.plain_result("此命令仅限管理员使用")
            return
        
        try:
            test_results = [Plain("🔍 Grok视频生成插件测试结果\n" + "="*30 + "\n\n")]
            
            if not self.api_key:
                test_results.append(Plain("❌ API密钥未配置\n"))
            else:
                test_results.append(Plain("✅ API密钥已配置\n"))
            
            test_results.append(Plain(f"📡 API地址: {self.api_url}\n"))
            test_results.append(Plain(f"📁 视频存储目录: {self.videos_dir}\n"))
            
            if self.enabled:
                test_results.append(Plain("✅ 功能已启用\n"))
            else:
                test_results.append(Plain("❌ 功能已禁用\n"))
            
            test_results.append(Plain(f"💾 强制本地文件发送模式: 启用 ({self.save_video_enabled})\n"))
            
            yield event.chain_result(test_results)
        
        except Exception as e:
            logger.error(f"测试命令异常: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @filter.command("grok帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助信息"""
        help_text = (
            "🎬 Grok视频生成插件帮助\n\n"
            "使用方法：\n"
            "1. 发送一张图片\n"
            "2. 引用该图片发送：/视频 <提示词>\n\n"
            "注意：当前配置为**强制本地文件发送**模式 (Video.fromFileSystem)，"
            "如果发送失败，通常是由于 Docker 部署下，Bot 容器和协议端无法共享文件路径导致。"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时调用"""
        self._rate_limit_locks.clear()
        logger.info("Grok视频生成插件已卸载")
