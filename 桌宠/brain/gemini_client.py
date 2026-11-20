import os
import re
import json
import time
import base64
import shutil
import tempfile
import subprocess
import sys
import threading

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
try:
    import requests
except ImportError:
    requests = None
from typing import Optional, Tuple, Dict, Any
from config_manager import ConfigManager
from log_manager import get_log_manager
from model_transport import empty_reason, stream_request
from .prompts import get_system_prompt, build_scene_prompt, build_interactive_prompt
from .context_memory import ContextMemoryManager

def is_valid_gemini_key(key: str) -> bool:
    """校验 Google Gemini API Key 是否符合规范 (支持传统 AIza 开头与新版 AQ. 授权密钥)"""
    if not key:
        return False
    k = key.strip()
    return bool(re.match(r"^(AIza[A-Za-z0-9_-]{20,}|AQ\.[A-Za-z0-9_-]{15,}|AQ[\w.-]{15,})$", k))

class AntigravityEngineHelper:
    """Google Antigravity SDK & agy CLI 调度器 (直接消耗用户的 Antigravity 会员订阅算力)"""
    def __init__(self, config_mgr=None, logger=None):
        self.config_mgr = config_mgr
        self.logger = logger
        self._agy_bin = self._find_agy_binary()

    def reload(self):
        self._agy_bin = self._find_agy_binary()

    def _find_agy_binary(self, custom_path: Optional[str] = None) -> Optional[str]:
        # 0. 优先检查显式传入或配置的自定义 CLI 路径
        if custom_path and os.path.exists(custom_path.strip()):
            return custom_path.strip()
        if self.config_mgr:
            cfg_p = (self.config_mgr.get("antigravity_cli_path") or "").strip()
            if cfg_p and os.path.exists(cfg_p):
                return cfg_p

        # 1. 检查系统环境变量 PATH
        for binary in ("agy.exe", "agy", "agy.cmd", "agy.bat", "antigravity.exe", "antigravity"):
            found = shutil.which(binary)
            if found and os.path.exists(found):
                return found

        # 2. Windows 常见安装与运行目录
        win_candidates = [
            os.path.expanduser("~/.gemini/bin/agy.exe"),
            os.path.expanduser("~/.gemini/bin/agy.cmd"),
            os.path.expanduser("~/.gemini/antigravity/bin/agy.exe"),
            os.path.expanduser("~/.antigravity/bin/agy.exe"),
            os.path.expanduser("~/AppData/Local/agy/bin/agy.exe"),
            os.path.expanduser("~/AppData/Local/Programs/antigravity/resources/bin/agy.exe"),
            os.path.expanduser("~/AppData/Local/Programs/antigravity/bin/agy.exe"),
            os.path.expanduser("~/AppData/Local/Programs/Antigravity/bin/agy.exe"),
            os.path.expanduser("~/AppData/Local/Programs/antigravity/Antigravity.exe"),
            os.path.expanduser("~/AppData/Roaming/Antigravity/bin/agy-node.cmd"),
            os.path.expanduser("~/AppData/Roaming/npm/agy.cmd"),
            r"C:\Program Files\Antigravityingy.exe",
            r"C:\Program Files\Google\Antigravityingy.exe",
        ]

        # 3. macOS 与 Linux 常见安装路径
        posix_candidates = [
            os.path.expanduser("~/.gemini/bin/agy"),
            os.path.expanduser("~/.antigravity/bin/agy"),
            "/usr/local/bin/agy",
            "/opt/homebrew/bin/agy",
            "/usr/bin/agy",
            os.path.expanduser("~/.local/bin/agy"),
            "/Applications/Antigravity.app/Contents/Resources/bin/agy",
            "/Applications/Antigravity.app/Contents/MacOS/Antigravity",
        ]

        candidates = win_candidates if IS_WIN else (posix_candidates + win_candidates)
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return None

    def is_available(self) -> bool:
        try:
            import google.antigravity
            return True
        except ImportError:
            pass
        return self._agy_bin is not None and os.path.exists(self._agy_bin)

    def test_connection(self, model: str = "gemini-3.8-flash-high") -> Tuple[bool, str]:
        if not self.is_available():
            return False, "未检测到 Antigravity 运行环境 (未找到 agy 命令行或 google-antigravity SDK)。"
        try:
            probe_model = "gemini-3.8-flash-low" if "gemini-3.8-flash" in model else model
            text = self.chat(
                sys_prompt="You are a helpful assistant.",
                user_prompt="Say 'ping' in one word.",
                image_b64=None,
                model=probe_model,
                timeout=60
            )
            if text:
                return True, f"连接成功！Google Antigravity 订阅算力通道 ({model}) 响应正常，已使用您的本地会员订阅配额！"
            return False, "Antigravity 服务未返回内容。"
        except Exception as e:
            return False, f"Antigravity 连接失败: {e}"

    def chat(
        self,
        sys_prompt: str,
        user_prompt: str,
        image_b64: Optional[str] = None,
        model: str = "gemini-3.8-flash-high",
        timeout: int = 75
    ) -> str:
        # 1. 优先尝试使用官方 google-antigravity Python SDK
        try:
            import asyncio
            from google.antigravity import Agent, LocalAgentConfig, types
            return self._call_sdk(sys_prompt, user_prompt, image_b64, model)
        except ImportError:
            pass

        # 2. 降级使用 Antigravity 原生 CLI (agy) 进行系统级桥接
        if self._agy_bin and os.path.exists(self._agy_bin):
            return self._call_cli(sys_prompt, user_prompt, image_b64, model, timeout=timeout)

        raise RuntimeError("未检测到已安装的 Antigravity (agy) 命令行或 google-antigravity SDK。")

    def _call_sdk(self, sys_prompt: str, user_prompt: str, image_b64: Optional[str], model: str) -> str:
        import asyncio
        from google.antigravity import Agent, LocalAgentConfig, types

        temp_img_path = None
        if image_b64:
            temp_dir = tempfile.gettempdir()
            temp_img_path = os.path.join(temp_dir, f"hagemi_screen_{int(time.time() * 1000)}.jpg")
            with open(temp_img_path, "wb") as f:
                f.write(base64.b64decode(image_b64))

        async def _run():
            config = LocalAgentConfig(
                system_instructions=sys_prompt,
                model=model
            )
            async with Agent(config) as agent:
                prompt_items = [user_prompt]
                if temp_img_path and os.path.exists(temp_img_path):
                    prompt_items.append(types.Image.from_file(temp_img_path))
                resp = await agent.chat(prompt_items)
                return await resp.text()

        try:
            return asyncio.run(_run())
        finally:
            if temp_img_path and os.path.exists(temp_img_path):
                try:
                    os.remove(temp_img_path)
                except Exception:
                    pass

    def _call_cli(self, sys_prompt: str, user_prompt: str, image_b64: Optional[str], model: str, timeout: int = 75) -> str:
        temp_img_path = None
        try:
            full_prompt = f"【系统人设与角色准则】\n{sys_prompt}\n\n【用户与桌面场景】\n{user_prompt}"
            if image_b64:
                temp_dir = tempfile.gettempdir()
                temp_img_path = os.path.join(temp_dir, f"hagemi_screen_{int(time.time() * 1000)}.jpg")
                with open(temp_img_path, "wb") as f:
                    f.write(base64.b64decode(image_b64))
                full_prompt += f"\n\n当前桌面截屏画面：@{temp_img_path}"

            use_shell = False
            if IS_WIN and self._agy_bin.lower().endswith((".cmd", ".bat")):
                use_shell = True

            cmd = [
                self._agy_bin,
                "--print", full_prompt,
                "--model", model,
                "--disable-slash-commands",
                "--dangerously-skip-permissions"
            ]
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                shell=use_shell,
                creationflags=CREATE_NO_WINDOW
            )
            if r.returncode == 0:
                out = r.stdout.strip()
                # 过滤可能夹带的 cli 内部日志或 thinking 标签
                lines = [l for l in out.splitlines() if not l.startswith("[") and not l.startswith("Thinking:")]
                clean_text = "\n".join(lines).strip() or out
                return clean_text
            else:
                err_msg = r.stderr.strip() or r.stdout.strip()
                raise RuntimeError(f"agy 执行失败 (code {r.returncode}): {err_msg[:160]}")
        finally:
            if temp_img_path and os.path.exists(temp_img_path):
                try:
                    os.remove(temp_img_path)
                except Exception:
                    pass


class VertexAuthHelper:
    """Google Cloud Vertex AI 认证与 Token 调度器
    支持:
    1. Service Account JSON 密钥文件 (优先 google-auth，降级使用 cryptography 自签名 JWT)
    2. 本地 gcloud CLI 或 ADC 自动凭据 (免配置文件)
    3. 直接输入 Bearer Access Token (ya29...) 或 Vertex Express Mode API Key (AQ... / AIzaSy...)
    """
    def __init__(self, logger=None):
        self.logger = logger
        self._cached_token = None
        self._token_expiry = 0
        self._cached_project = None

    def get_auth(
        self,
        creds_file: Optional[str] = None,
        project_id: Optional[str] = None,
        api_key: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        now = time.time()
        raw_key = (api_key or "").strip()

        # 1. 直接提供 OAuth2 Bearer Access Token (通常以 ya29. 开头)
        if raw_key.startswith("ya29."):
            proj = project_id or self._detect_gcloud_project()
            return raw_key, proj, None, None

        # 2. 传入了 Vertex Express Mode 或 GCP API Key (通常以 AIzaSy 或 AQ 开头)
        if raw_key and (raw_key.startswith("AIzaSy") or raw_key.startswith("AQ")):
            proj = project_id or self._detect_gcloud_project()
            return None, proj, raw_key, None

        # 3. 检查已缓存且尚未过期的 Bearer Token (留出 60s 缓冲)
        if self._cached_token and now < (self._token_expiry - 60):
            proj = project_id or self._cached_project or self._detect_gcloud_project()
            return self._cached_token, proj, None, None

        # 4. 指定了服务账号 JSON 密钥文件
        target_file = creds_file.strip() if creds_file else ""
        if target_file and os.path.exists(target_file):
            token, sa_proj, err = self._token_from_service_account(target_file)
            if token:
                self._cached_token = token
                self._token_expiry = now + 3500
                self._cached_project = sa_proj or project_id
                resolved_proj = project_id or sa_proj or self._cached_project
                return token, resolved_proj, None, None
            if err:
                return None, project_id, None, err

        # 5. 尝试通过 google-auth 读取本地 ADC (Application Default Credentials)
        token, adc_proj = self._token_from_adc()
        if token:
            self._cached_token = token
            self._token_expiry = now + 3500
            self._cached_project = adc_proj or project_id
            return token, (project_id or adc_proj), None, None

        # 6. 尝试通过 本地 gcloud CLI 自动获取 Access Token
        token, gc_proj = self._token_from_gcloud()
        if token:
            self._cached_token = token
            self._token_expiry = now + 3000
            self._cached_project = gc_proj or project_id
            return token, (project_id or gc_proj), None, None

        return None, project_id, None, (
            "未找到有效的 Vertex AI 凭据。请在控制台指定 Service Account JSON 密钥文件、"
            "在终端通过 'gcloud auth login' 登录，或填入 API Key。"
        )

    def _token_from_service_account(self, creds_file: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        try:
            with open(creds_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            sa_proj = data.get("project_id")

            # 优先尝试 google.oauth2.service_account 官方库
            try:
                from google.oauth2 import service_account
                import google.auth.transport.requests
                creds = service_account.Credentials.from_service_account_info(
                    data,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                creds.refresh(google.auth.transport.requests.Request())
                return creds.token, sa_proj or getattr(creds, "project_id", None), None
            except ImportError:
                pass
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"google-auth 解析服务账号失败，尝试内置签名降级: {e}")

            # 降级：利用 cryptography 库自签名 JWT 并向 Google OAuth2 端点兑换 Access Token
            try:
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import padding
                from cryptography.hazmat.primitives.serialization import load_pem_private_key

                client_email = data.get("client_email")
                private_key_pem = data.get("private_key", "").encode("utf-8")
                token_uri = data.get("token_uri", "https://oauth2.googleapis.com/token")

                if not client_email or not private_key_pem:
                    return None, sa_proj, "服务账号 JSON 格式不完整 (缺少 client_email 或 private_key)。"

                now = int(time.time())
                header = {"alg": "RS256", "typ": "JWT"}
                payload = {
                    "iss": client_email,
                    "scope": "https://www.googleapis.com/auth/cloud-platform",
                    "aud": token_uri,
                    "exp": now + 3600,
                    "iat": now
                }

                def b64url(b: bytes) -> str:
                    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")

                sig_input = f"{b64url(json.dumps(header).encode('utf-8'))}.{b64url(json.dumps(payload).encode('utf-8'))}".encode("utf-8")
                pkey = load_pem_private_key(private_key_pem, password=None)
                signature = pkey.sign(sig_input, padding.PKCS1v15(), hashes.SHA256())
                signed_jwt = f"{sig_input.decode('utf-8')}.{b64url(signature)}"

                r = requests.post(
                    token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": signed_jwt
                    },
                    timeout=12
                )
                if r.status_code == 200:
                    token_data = r.json()
                    return token_data.get("access_token"), sa_proj, None
                else:
                    return None, sa_proj, f"向 Google 兑换 OAuth Token 失败 ({r.status_code}): {r.text[:120]}"
            except ImportError:
                return None, sa_proj, "未安装 google-auth 或 cryptography 依赖库，无法解析服务账号密钥。"
            except Exception as e:
                return None, sa_proj, f"服务账号自签名异常: {e}"

        except Exception as e:
            return None, None, f"读取服务账号 JSON 文件异常: {e}"

    def _token_from_adc(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            import google.auth
            import google.auth.transport.requests
            creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(google.auth.transport.requests.Request())
            return creds.token, project
        except Exception:
            return None, None

    def _token_from_gcloud(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            cmd = "gcloud.cmd auth print-access-token" if os.name == "nt" else "gcloud auth print-access-token"
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW
            )
            if r.returncode == 0:
                token = r.stdout.strip()
                if token and not token.startswith("ERROR"):
                    proj = self._detect_gcloud_project()
                    return token, proj
        except Exception:
            pass
        return None, None

    def _detect_gcloud_project(self) -> Optional[str]:
        try:
            cmd = "gcloud.cmd config get-value project" if os.name == "nt" else "gcloud config get-value project"
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=6,
                creationflags=CREATE_NO_WINDOW
            )
            p = r.stdout.strip()
            if p and "(unset)" not in p and not p.startswith("ERROR"):
                return p
        except Exception:
            pass
        return None


class GeminiCritiqueEngine:
    def __init__(self):
        self._model_result = threading.local()
        self.observation_callback = None
        # 扩展只替换传输，保留原提示词、场景与持久记忆。None 完整沿用原服务商。
        self.request_handler = None
        self.config_mgr = ConfigManager()
        self.context_memory = ContextMemoryManager()
        self.logger = get_log_manager()
        self.vertex_auth = VertexAuthHelper(logger=self.logger)
        self.antigravity_helper = AntigravityEngineHelper(config_mgr=self.config_mgr, logger=self.logger)

    @property
    def provider(self) -> str:
        prov = self.config_mgr.get("api_provider", "").strip().lower()
        if prov in ("antigravity", "agy", "google_antigravity"):
            return "antigravity"
        if prov in ("vertex", "vertex_ai", "google_cloud", "gcp"):
            return "vertex"
        if prov in ("gemini", "google"):
            return "gemini"
        if prov in ("openai_compatible", "openai", "openrouter"):
            return "openai_compatible"

        # 兜底识别
        key = self.api_key
        if key.startswith("AIza") or key.startswith("AQ"):
            return "gemini"
        if key.startswith("sk-"):
            return "openai_compatible"
        return "gemini"

    @property
    def api_key(self) -> str:
        return self.config_mgr.get("api_key", "").strip()

    @property
    def model(self) -> str:
        if self.provider == "antigravity":
            default_model = "gemini-3.8-flash-high"
        elif self.provider == "vertex":
            default_model = "gemini-4-argon"
        else:
            default_model = "gemini-3.8-flash"
        return self.config_mgr.get("model", default_model).strip()

    @property
    def custom_url(self) -> str:
        return self.config_mgr.get("custom_api_url", "https://openrouter.ai/api/v1").strip()

    @property
    def vertex_project_id(self) -> str:
        return self.config_mgr.get("vertex_project_id", "").strip()

    @property
    def vertex_location(self) -> str:
        return self.config_mgr.get("vertex_location", "global").strip()

    @property
    def vertex_credentials_file(self) -> str:
        return self.config_mgr.get("vertex_credentials_file", "").strip()

    @property
    def timeout(self) -> int:
        try:
            return max(5, int(self.config_mgr.get("api_timeout", 35)))
        except (ValueError, TypeError):
            return 35

    def test_connection(
        self,
        key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        custom_url: Optional[str] = None,
        vertex_location: Optional[str] = None,
        vertex_project_id: Optional[str] = None,
        vertex_creds_file: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> Tuple[bool, str]:
        """测试各渠道的算力可用性。"""
        req_timeout = timeout or self.timeout
        test_prov = (provider or self.provider).strip().lower()
        test_key = key.strip() if key is not None else self.api_key
        test_model = (model or self.model).strip()
        test_url = (custom_url or self.custom_url).strip()

        if test_prov in ("antigravity", "agy", "google_antigravity"):
            self.antigravity_helper.reload()
            return self.antigravity_helper.test_connection(model=test_model)

        if test_prov in ("vertex_ai", "google_cloud", "gcp"):
            test_prov = "vertex"

        if test_prov == "openai_compatible":
            if not test_key:
                return False, "API Key 不能为空！"
            base = test_url.rstrip("/")
            endpoint = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {test_key}",
                "HTTP-Referer": "https://github.com/LYW199911/DesktopGeminiGirl",
                "X-Title": "HaGemi Desktop Pet"
            }
            payload = {
                "model": test_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5
            }
            try:
                r = requests.post(endpoint, json=payload, headers=headers, timeout=req_timeout)
                if r.status_code == 200:
                    return True, f"连接成功！{test_model} 算力响应正常。"
                else:
                    return False, f"API 返回错误 ({r.status_code}): {r.text[:120]}"
            except Exception as e:
                return False, f"网络连接超时或错误: {e}"

        elif test_prov == "vertex":
            loc = (vertex_location or self.vertex_location).strip() or "global"
            creds_f = vertex_creds_file if vertex_creds_file is not None else self.vertex_credentials_file
            proj = (vertex_project_id if vertex_project_id is not None else self.vertex_project_id).strip()

            token, resolved_proj, auth_key, err = self.vertex_auth.get_auth(
                creds_file=creds_f,
                project_id=proj,
                api_key=test_key
            )
            if not token and not auth_key:
                return False, err or "未能获取有效的 Vertex AI 认证凭据！"
            if not resolved_proj:
                return False, "未能识别到 Google Cloud 项目 ID (Project ID)！请在输入框中填入 GCP Project ID。"

            host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
            url = f"https://{host}/v1beta1/projects/{resolved_proj}/locations/{loc}/publishers/google/models/{test_model}:generateContent"
            if auth_key and not token:
                url += f"?key={auth_key}"

            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if auth_key and not token:
                headers["x-goog-api-key"] = auth_key

            payload = {
                "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 10}
            }
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=req_timeout)
                if r.status_code == 200:
                    return True, f"连接成功！Google Cloud Vertex AI ({test_model}) [项目: {resolved_proj}, 区域: {loc}] 响应正常！"
                else:
                    err_text = r.text[:180]
                    if r.status_code == 403:
                        return False, f"Vertex AI 权限受限 (403): 请检查该服务账号是否具备 roles/aiplatform.user 权限并已关联有效结算账户。\n详情: {err_text}"
                    elif r.status_code == 404:
                        return False, f"Vertex AI 资源未找到 (404): 模型 {test_model} 可能在该区域暂未上线，推荐将区域调整为 global。\n详情: {err_text}"
                    return False, f"Vertex AI 返回错误 ({r.status_code}): {err_text}"
            except Exception as e:
                return False, f"Vertex AI 网络连接超时或错误: {e}"

        else:
            # Google Gemini AI Studio native (对照官方 AQ. 开头密钥标准 cURL 格式)
            if not test_key:
                return False, "API Key 不能为空！"

            # 兼容转换：若配置中残留了已下线的旧版模型或 Antigravity 专属后缀，自动修正为现行主力模型
            if any(tag in test_model for tag in ("2.5", "2.0", "1.5")) or test_model.startswith("gemini-3.8-flash-"):
                test_model = "gemini-3.8-flash"

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{test_model}:generateContent?key={test_key}"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": test_key
            }
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": "ping"}
                        ]
                    }
                ]
            }
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=req_timeout)
                if r.status_code == 200:
                    return True, f"连接成功！Google Gemini ({test_model}) 算力响应正常。"
                elif r.status_code == 404:
                    return False, f"模型未找到 (404): AI Studio 暂不支持该模型 ({test_model})，请在设置中选择 'gemini-3.8-flash' 或 'gemini-4-argon'！\n详情: {r.text[:140]}"
                elif r.status_code == 429:
                    return False, f"算力超限/服务器繁忙 (429): Google AI Studio 免费额度已达限额 (每分钟 10~15 次) 或高峰期算力繁忙。建议稍后重试或切换至 Antigravity 订阅通道！\n详情: {r.text[:140]}"
                elif r.status_code == 400:
                    return False, f"参数或密钥错误 (400): 请检查 API Key 是否准确无误。\n详情: {r.text[:140]}"
                elif r.status_code == 403:
                    return False, f"权限或地区受限 (403): 请检查该 API Key 是否启用，或当前网络节点是否支持 Google AI Studio。\n详情: {r.text[:140]}"
                else:
                    return False, f"Gemini API 返回错误 ({r.status_code}): {r.text[:140]}"
            except Exception as e:
                return False, f"网络连接超时或错误: {e}"

    def generate_critique(
        self,
        scene: str,
        window_title: str,
        process_name: str,
        image_b64: Optional[str] = None
    ) -> str:
        self._model_result.success = False
        observation_callback = self.observation_callback
        observed_at = time.time()
        pet_name = self.config_mgr.get("pet_name", "哈Gemi")

        request_handler = self.request_handler
        if request_handler is None and self.provider not in ("vertex", "antigravity") and not self.api_key:
            return f"[curious] 喵？主人还没有在控制台配置 API Key 喵！快去填上密钥~"

        catchphrases = self.config_mgr.get("catchphrases", ["喵~", "喵？！", "哼~", "盯——"])
        custom_prompt = self.config_mgr.get("custom_system_prompt", "")
        persona_style = self.config_mgr.get("persona_style", "元气毒舌 (默认)")

        history_context = self.context_memory.get_recent_context(scene, window_title, max_items=2)
        if history_context:
            self.logger.memory(f"提取到 {len(history_context)} 条前情记忆承接上下文: \"{history_context[-1][:20]}...\"")

        sys_prompt = get_system_prompt(pet_name, catchphrases, custom_prompt, persona_style=persona_style)
        user_prompt = build_scene_prompt(
            scene, window_title, process_name,
            pet_name=pet_name,
            persona_style=persona_style,
            history_context=history_context
        )

        if request_handler is not None:
            text = request_handler(sys_prompt, user_prompt, image_b64, pet_name)
            self._model_result.success = bool(text)
        elif self.provider == "openai_compatible":
            text = self._call_openai_compatible(sys_prompt, user_prompt, image_b64, pet_name)
        elif self.provider == "vertex":
            text = self._call_vertex_ai(sys_prompt, user_prompt, image_b64, pet_name)
        elif self.provider == "antigravity":
            text = self._call_antigravity(sys_prompt, user_prompt, image_b64, pet_name)
        else:
            text = self._call_gemini_native(sys_prompt, user_prompt, image_b64, pet_name)

        if text and not text.startswith("[relieved] 喵呜！"):
            self.context_memory.record_critique(scene, window_title, text, pet_name=pet_name)

        if image_b64 and self._model_result.success and observation_callback:
            observation_callback({'text': text, 'observed_at': observed_at, 'scene': scene,
                                       'title': window_title, 'process': process_name})
        return text

    def _verified_text(self, text):
        self._model_result.success = bool(text.strip())
        return text

    def _stream_text(self, url, payload, headers, *, gemini):
        result = stream_request(requests, url, payload, headers, self.timeout, gemini=gemini,
                                include_usage=self.config_mgr.get('api_stream_usage', True))
        if gemini:
            text = ''.join(p.get('text', '') for r in result.get('candidates', [])[:1]
                           for p in r.get('content', {}).get('parts', []) if not p.get('thought'))
        else:
            text = ((result.get('choices') or [{}])[0].get('message') or {}).get('content')
        if not isinstance(text, str) or not text.strip():
            raise ValueError(empty_reason(result, gemini))
        return self._verified_text(text.replace('"', '').replace('“', '').replace('”', '').strip())

    def _stream_failure(self, error):
        # 只接收传输层已净化的错误；前缀使原记忆流程跳过失败提示。
        self._model_result.success = False
        self.logger.error('模型请求失败：' + str(error))
        return '[relieved] 喵呜！' + str(error)

    def generate_interactive_reply(
        self,
        scene: str,
        window_title: str,
        process_name: str,
        user_comment: str,
        image_b64: Optional[str] = None
    ) -> str:
        pet_name = self.config_mgr.get("pet_name", "哈Gemi")

        request_handler = self.request_handler
        if request_handler is None and self.provider not in ("vertex", "antigravity") and not self.api_key:
            return f"[curious] 喵？主人还没有在控制台配置 API Key 喵！快去填上密钥~"

        catchphrases = self.config_mgr.get("catchphrases", ["喵~", "喵？！", "哼~", "盯——"])
        custom_prompt = self.config_mgr.get("custom_system_prompt", "")
        persona_style = self.config_mgr.get("persona_style", "元气毒舌 (默认)")

        self.context_memory.record_user_comment(scene, window_title, user_comment)
        history_context = self.context_memory.get_recent_context(scene, window_title, max_items=3)
        self.logger.memory(f"主人在气泡吐槽: \"{user_comment}\" (上下文记忆 {len(history_context)} 条)")

        sys_prompt = get_system_prompt(pet_name, catchphrases, custom_prompt, persona_style=persona_style)
        user_prompt = build_interactive_prompt(
            scene, window_title, process_name,
            user_comment=user_comment,
            pet_name=pet_name,
            persona_style=persona_style,
            history_context=history_context
        )

        if request_handler is not None:
            text = request_handler(sys_prompt, user_prompt, image_b64, pet_name)
        elif self.provider == "openai_compatible":
            text = self._call_openai_compatible(sys_prompt, user_prompt, image_b64, pet_name)
        elif self.provider == "vertex":
            text = self._call_vertex_ai(sys_prompt, user_prompt, image_b64, pet_name)
        elif self.provider == "antigravity":
            text = self._call_antigravity(sys_prompt, user_prompt, image_b64, pet_name)
        else:
            text = self._call_gemini_native(sys_prompt, user_prompt, image_b64, pet_name)

        if text and not text.startswith("[relieved] 喵呜！"):
            self.context_memory.record_critique(scene, window_title, text, pet_name=pet_name)

        return text

    def _call_antigravity(self, sys_prompt: str, user_prompt: str, image_b64: Optional[str], pet_name: str) -> str:
        try:
            text = self.antigravity_helper.chat(
                sys_prompt=sys_prompt,
                user_prompt=user_prompt,
                image_b64=image_b64,
                model=self.model or "gemini-3.8-flash-high",
                timeout=max(self.timeout, 60)
            )
            if text:
                text = text.replace('"', '').replace('“', '').replace('”', '').strip()
                return self._verified_text(text)
            return f"[curious] 盯——主人当前的操作让本{pet_name}都陷入思考了喵..."
        except Exception as e:
            print(f"[AntigravityEngine] Request failed: {e}", flush=True)
            self.logger.error(f"Antigravity 订阅通道调用异常: {e}")
            return f"[relieved] 喵呜~Antigravity 本地订阅算力连接超时或受到干扰喵！"

    def _call_gemini_native(self, sys_prompt: str, user_prompt: str, image_b64: Optional[str], pet_name: str) -> str:
        model = self.model
        if any(tag in model for tag in ("2.5", "2.0", "1.5")) or model.startswith("gemini-3.8-flash-"):
            model = "gemini-3.8-flash"

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"

        parts = []
        if image_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": image_b64
                }
            })
        parts.append({"text": user_prompt})

        gen_config = {
            "temperature": float(self.config_mgr.get("temperature", 0.88)),
            "maxOutputTokens": 2048
        }
        # Flash / 3.8 / 2.5: set thinkingBudget to 0 for instant desk pet responses without truncating tokens
        if "flash" in model.lower() or "2.5" in model or "3.8" in model:
            gen_config["thinkingConfig"] = {
                "thinkingBudget": 0
            }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts
                }
            ],
            "system_instruction": {
                "parts": [
                    {"text": sys_prompt}
                ]
            },
            "generationConfig": gen_config
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key
        }

        try:
            return self._stream_text(url, payload, headers, gemini=True)
        except ValueError as exc:
            return self._stream_failure(exc)
        except Exception:
            return self._stream_failure('模型响应格式异常，请检查接口是否兼容Gemini流式协议')

    def _call_vertex_ai(self, sys_prompt: str, user_prompt: str, image_b64: Optional[str], pet_name: str) -> str:
        loc = self.vertex_location.strip() or "global"
        token, proj_id, auth_key, err = self.vertex_auth.get_auth(
            creds_file=self.vertex_credentials_file,
            project_id=self.vertex_project_id,
            api_key=self.api_key
        )
        if not token and not auth_key:
            return f"[curious] 喵？Vertex AI 凭据未就绪：{err or '请在控制台设置密钥'}喵！"
        if not proj_id:
            return f"[curious] 喵？未识别到 Google Cloud Project ID，快去控制台填上喵~"

        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = f"https://{host}/v1beta1/projects/{proj_id}/locations/{loc}/publishers/google/models/{self.model}:generateContent"
        if auth_key and not token:
            url += f"?key={auth_key}"

        parts = []
        if image_b64:
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": image_b64
                }
            })
        parts.append({"text": user_prompt})

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts
                }
            ],
            "systemInstruction": {
                "parts": [
                    {"text": sys_prompt}
                ]
            },
            "generationConfig": {
                "temperature": float(self.config_mgr.get("temperature", 0.88)),
                "maxOutputTokens": 2048,
                "thinkingConfig": {"thinkingBudget": 0} if "flash" in self.model.lower() else {}
            }
        }
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if auth_key and not token:
            headers["x-goog-api-key"] = auth_key

        try:
            return self._stream_text(url, payload, headers, gemini=True)
        except ValueError as exc:
            return self._stream_failure(exc)
        except Exception:
            return self._stream_failure('Vertex模型响应格式异常，请检查接口配置')

    def _call_openai_compatible(self, sys_prompt: str, user_prompt: str, image_b64: Optional[str], pet_name: str) -> str:
        base = self.custom_url.rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else f"{base}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/LYW199911/DesktopGeminiGirl",
            "X-Title": "HaGemi Desktop Pet"
        }

        user_content = []
        if image_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}"
                }
            })
        user_content.append({
            "type": "text",
            "text": user_prompt
        })

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content}
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.config_mgr.get("temperature", 0.88)),
            "max_tokens": 1500
        }

        try:
            return self._stream_text(endpoint, payload, headers, gemini=False)
        except ValueError as exc:
            return self._stream_failure(exc)
        except Exception:
            return self._stream_failure('模型响应格式异常，请检查接口是否兼容OpenAI流式协议')
