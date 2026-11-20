import os
import sys
import webbrowser
import tempfile
import threading
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QTextBrowser, QProgressBar, QFrame, QMessageBox, QApplication
)
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QFont, QIcon

try:
    from update_checker import (
        UpdateChecker, GITHUB_REPO_OWNER, GITHUB_REPO_NAME,
        extract_update_zip, launch_auto_installer
    )
    from version import VERSION
except ImportError:
    from gemini_desktop_pet.update_checker import (
        UpdateChecker, GITHUB_REPO_OWNER, GITHUB_REPO_NAME,
        extract_update_zip, launch_auto_installer
    )
    from gemini_desktop_pet.version import VERSION

class UpdateDialog(QDialog):
    def __init__(self, is_dynamic: bool = False, parent=None):
        super().__init__(parent)
        self.is_dynamic = is_dynamic
        self.current_version = f"{VERSION}-dynamic" if is_dynamic else VERSION
        self._is_auto_updating = False
        self._is_newer = False
        self.checker = UpdateChecker(current_version=VERSION, parent=self)

        self.setWindowTitle("哈Gemi · 检查更新与公告中心")
        self.setMinimumSize(680, 520)
        self.resize(720, 560)

        # Style matching Anime Dark Studio theme
        self.setStyleSheet("""
            QDialog {
                background-color: #1a162b;
                color: #e4e0f0;
                font-family: 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', sans-serif;
            }
            QLabel {
                color: #e4e0f0;
            }
            QTextBrowser {
                background-color: #151124;
                color: #d6d0e6;
                border: 1px solid #362d52;
                border-radius: 8px;
                padding: 14px;
                font-size: 13px;
                line-height: 140%;
            }
            QPushButton {
                background-color: #ff4e88;
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 8px 18px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #ff689a;
            }
            QPushButton:pressed {
                background-color: #e03b73;
            }
            QPushButton#SecondaryBtn {
                background-color: #2b2342;
                border: 1px solid #4a3e6b;
                color: #c9c3dd;
            }
            QPushButton#SecondaryBtn:hover {
                background-color: #3b305c;
                color: #ffffff;
            }
            QProgressBar {
                border: 1px solid #362d52;
                border-radius: 6px;
                background-color: #171326;
                text-align: center;
                color: #ffffff;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #ff4e88;
                border-radius: 5px;
            }
        """)

        self._init_ui()
        self._connect_signals()
        self.check_update()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        # 1. Header Card
        header = QHBoxLayout()
        icon_lbl = QLabel("📢")
        icon_lbl.setStyleSheet("font-size: 26px; padding-right: 6px;")
        header.addWidget(icon_lbl)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_lbl = QLabel("哈Gemi 版本与更新公告中心")
        title_lbl.setStyleSheet("font-size: 17px; font-weight: bold; color: #ff7aa6;")
        self.lbl_status = QLabel("正在从 GitHub 获取最新版本与公告信息...")
        self.lbl_status.setStyleSheet("color: #a49cb8; font-size: 12px;")
        title_box.addWidget(title_lbl)
        title_box.addWidget(self.lbl_status)
        header.addLayout(title_box)

        header.addStretch()

        # Version tags
        self.tag_current = QLabel(f"当前版本: {self.current_version}")
        self.tag_current.setStyleSheet("""
            background-color: #2b2342;
            color: #d1c8e8;
            border: 1px solid #4a3e6b;
            border-radius: 6px;
            padding: 4px 10px;
            font-size: 11px;
            font-weight: bold;
        """)
        header.addWidget(self.tag_current)

        layout.addLayout(header)

        # 2. Release Notes & Announcement Browser
        self.text_browser = QTextBrowser(self)
        self.text_browser.setOpenExternalLinks(True)
        self.text_browser.setHtml("<p style='color:#a49cb8; text-align:center; padding-top:40px;'>正在连接 GitHub 探测最新版本发布日志，请稍候喵~ ✨</p>")
        layout.addWidget(self.text_browser)

        # 3. Download Progress Bar (Hidden initially)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(18)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # 4. Bottom Button Row
        btn_bar = QHBoxLayout()

        self.btn_check_again = QPushButton("🔄 重新检查", self)
        self.btn_check_again.setObjectName("SecondaryBtn")
        self.btn_check_again.clicked.connect(self.check_update)
        btn_bar.addWidget(self.btn_check_again)

        btn_bar.addStretch()

        self.btn_open_web = QPushButton("🌐 GitHub 发布页", self)
        self.btn_open_web.setObjectName("SecondaryBtn")
        self.btn_open_web.clicked.connect(self._open_github_release)
        btn_bar.addWidget(self.btn_open_web)

        self.btn_download = QPushButton("💾 仅下载安装包", self)
        self.btn_download.setObjectName("SecondaryBtn")
        self.btn_download.clicked.connect(self._start_direct_download)
        self.btn_download.setEnabled(False)
        btn_bar.addWidget(self.btn_download)

        self.btn_auto_update = QPushButton("⚡ 一键自动安装并重启", self)
        self.btn_auto_update.clicked.connect(self._start_auto_update)
        self.btn_auto_update.setEnabled(False)
        btn_bar.addWidget(self.btn_auto_update)

        self.btn_close = QPushButton("关闭", self)
        self.btn_close.setObjectName("SecondaryBtn")
        self.btn_close.clicked.connect(self.close)
        btn_bar.addWidget(self.btn_close)

        layout.addLayout(btn_bar)

    def _connect_signals(self):
        self.checker.check_finished.connect(self._on_check_finished)
        self.checker.download_progress.connect(self._on_download_progress)
        self.checker.download_finished.connect(self._on_download_finished)
        self.checker.download_error.connect(self._on_download_error)
        self.checker.extract_finished.connect(self._on_extract_ready)
        self.checker.extract_error.connect(self._on_extract_error)

    def check_update(self):
        self.lbl_status.setText("正在连接 GitHub 检查最新版本与公告...")
        self.btn_check_again.setEnabled(False)
        self.btn_download.setEnabled(False)
        self.btn_auto_update.setEnabled(False)
        self.checker.check_async()

    def _on_check_finished(self, data: dict, is_newer: bool, err_msg: str):
        self.btn_check_again.setEnabled(True)
        if err_msg:
            self.lbl_status.setText(f"❌ 检查更新失败: {err_msg}")
            self.text_browser.setHtml(f"""
                <div style='padding:20px; color:#ff6b8b;'>
                    <h3>⚠️ 无法连接至 GitHub Releases 检查更新</h3>
                    <p>错误原因: {err_msg}</p>
                    <p>请检查网络连接或稍后重试，也可以直接访问官方仓库：</p>
                    <p><a href='https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}' style='color:#ff7aa6;'>https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}</a></p>
                </div>
            """)
            return

        remote_tag = data.get("tag_name", "")
        release_name = data.get("name", remote_tag)
        body = data.get("body", "暂无详细说明。")
        self.release_url = data.get("html_url", f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases")

        # Find matching asset for download based on platform and edition
        self.target_asset = None
        target_edition = "Dynamic" if self.is_dynamic else "Classic"
        target_platform = "windows" if sys.platform == "win32" else "macos"

        # 1. Match both edition and platform
        for asset in data.get("assets", []):
            name_lower = asset.get("name", "").lower()
            if target_edition.lower() in name_lower and target_platform in name_lower:
                self.target_asset = asset
                break

        # 2. Match edition only if platform wasn't matched
        if not self.target_asset:
            for asset in data.get("assets", []):
                name_lower = asset.get("name", "").lower()
                if target_edition.lower() in name_lower:
                    self.target_asset = asset
                    break

        if not self.target_asset and data.get("assets"):
            self.target_asset = data["assets"][0]

        self._is_newer = is_newer
        target_display = "动态版" if self.is_dynamic else "经典版"
        size_str = f" ({self.target_asset['size']/(1024*1024):.1f}MB)" if self.target_asset and 'size' in self.target_asset else ""

        if is_newer:
            self.lbl_status.setText(f"🎉 发现新版本: {remote_tag}！建议及时更新体验新特性~")
            self.btn_auto_update.setEnabled(self.target_asset is not None)
            self.btn_download.setEnabled(self.target_asset is not None)
            if self.target_asset:
                self.btn_auto_update.setText(f"⚡ 自动升级 {target_display}{size_str}")
                self.btn_download.setText("💾 仅下载安装包")
        else:
            self.lbl_status.setText(f"✨ 当前已是最新版本 ({remote_tag}) 喵！以下为最新公告：")
            self.btn_auto_update.setEnabled(self.target_asset is not None)
            self.btn_download.setEnabled(self.target_asset is not None)
            if self.target_asset:
                self.btn_auto_update.setText(f"⚡ 一键重新安装并重启{size_str}")
                self.btn_download.setText("💾 仅下载安装包")

        # Format markdown body to clean HTML
        html_body = self._markdown_to_html(body)
        full_html = f"""
        <div style='font-family: sans-serif;'>
            <div style='border-bottom: 1px solid #362d52; padding-bottom: 10px; margin-bottom: 14px;'>
                <h2 style='color:#ff7aa6; margin:0 0 6px 0;'>{release_name}</h2>
                <span style='color:#8c82a0; font-size:11px;'>发布标签: {remote_tag} · 发布时间: {data.get("published_at", "")[:10]}</span>
            </div>
            {html_body}
        </div>
        """
        self.text_browser.setHtml(full_html)

    def _markdown_to_html(self, text: str) -> str:
        """Converts basic markdown (headers, bullets, bold, links) into styled HTML."""
        lines = text.splitlines()
        html_lines = []
        in_list = False

        for line in lines:
            line_str = line.strip()
            if not line_str:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                continue

            # Headers
            if line_str.startswith("### "):
                if in_list: html_lines.append("</ul>"); in_list = False
                html_lines.append(f"<h3 style='color:#ff8eb3; margin-top:14px; margin-bottom:6px;'>{line_str[4:]}</h3>")
            elif line_str.startswith("## "):
                if in_list: html_lines.append("</ul>"); in_list = False
                html_lines.append(f"<h2 style='color:#ff7aa6; margin-top:16px; margin-bottom:8px;'>{line_str[3:]}</h2>")
            elif line_str.startswith("# "):
                if in_list: html_lines.append("</ul>"); in_list = False
                html_lines.append(f"<h1 style='color:#ff4e88; margin-top:18px; margin-bottom:10px;'>{line_str[2:]}</h1>")
            elif line_str.startswith("- ") or line_str.startswith("* "):
                if not in_list:
                    html_lines.append("<ul style='margin-top:4px; margin-bottom:6px; padding-left:20px;'>")
                    in_list = True
                content = self._format_inline_md(line_str[2:])
                html_lines.append(f"<li style='margin-bottom:4px; color:#e4e0f0;'>{content}</li>")
            elif line_str.startswith("1. ") or line_str.startswith("2. ") or line_str.startswith("3. ") or line_str.startswith("4. ") or line_str.startswith("5. "):
                if in_list: html_lines.append("</ul>"); in_list = False
                content = self._format_inline_md(line_str[3:])
                html_lines.append(f"<p style='margin:4px 0; padding-left:8px; color:#e4e0f0;'><b>{line_str[:2]}</b> {content}</p>")
            else:
                if in_list: html_lines.append("</ul>"); in_list = False
                content = self._format_inline_md(line_str)
                html_lines.append(f"<p style='margin:4px 0; color:#c9c3dd;'>{content}</p>")

        if in_list:
            html_lines.append("</ul>")

        return "\n".join(html_lines)

    def _format_inline_md(self, text: str) -> str:
        # Bold **text**
        import re
        text = re.sub(r'\*\*(.*?)\*\*', r"<b style='color:#ffffff;'>\1</b>", text)
        # Code `code`
        text = re.sub(r'`(.*?)`', r"<code style='background-color:#261f3b; color:#ff94ba; padding:1px 5px; border-radius:4px;'>\1</code>", text)
        # Links [title](url)
        text = re.sub(r'\[(.*?)\]\((.*?)\)', r"<a href='\2' style='color:#ff7aa6; text-decoration:none;'>\1</a>", text)
        return text

    def _open_github_release(self):
        url = getattr(self, "release_url", f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases")
        webbrowser.open(url)

    def _start_auto_update(self):
        if not self.target_asset:
            self._open_github_release()
            return

        download_url = self.target_asset.get("browser_download_url")
        filename = self.target_asset.get("name", "Ha-Gemi-Update.zip")
        dest_dir = os.path.join(tempfile.gettempdir(), "HaGemi_AutoUpdate")

        self._is_auto_updating = True
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.btn_auto_update.setEnabled(False)
        self.btn_download.setEnabled(False)
        self.btn_check_again.setEnabled(False)
        self.lbl_status.setText(f"⏳ 正在下载最新升级包 ({filename})...")

        self.checker.download_asset_async(download_url, dest_dir, filename)

    def _start_direct_download(self):
        if not self.target_asset:
            self._open_github_release()
            return

        download_url = self.target_asset.get("browser_download_url")
        filename = self.target_asset.get("name", "Ha-Gemi-Update.zip")
        dest_dir = os.path.expanduser("~/Downloads")

        self._is_auto_updating = False
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.btn_auto_update.setEnabled(False)
        self.btn_download.setEnabled(False)
        self.btn_check_again.setEnabled(False)
        self.lbl_status.setText(f"⏳ 正在下载 {filename} 到下载文件夹，请稍候...")

        self.checker.download_asset_async(download_url, dest_dir, filename)

    def _on_download_progress(self, percent: int):
        self.progress_bar.setValue(percent)

    def _on_download_finished(self, saved_path: str):
        self.progress_bar.setValue(100)

        if not self._is_auto_updating:
            self.btn_auto_update.setEnabled(True)
            self.btn_download.setEnabled(True)
            self.btn_check_again.setEnabled(True)
            self.lbl_status.setText(f"🎉 下载完成！文件已存放在: {saved_path}")

            reply = QMessageBox.information(
                self,
                "下载成功",
                f"新版本压缩包已成功下载至您的【下载】文件夹！\n文件路径: {saved_path}\n\n是否立即打开所在文件夹查看？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                if sys.platform == "win32":
                    os.system(f'explorer.exe /select,"{saved_path}"')
                elif sys.platform == "darwin":
                    os.system(f'open -R "{saved_path}"')
            return

        # Auto updater workflow
        self.lbl_status.setText("📦 正在解压新版本并准备覆盖安装，请稍候...")
        extract_root = os.path.join(tempfile.gettempdir(), "HaGemi_Extracted")
        self.checker.extract_asset_async(saved_path, extract_root)

    def _on_extract_ready(self, src_dir: str):
        self.btn_auto_update.setEnabled(True)
        self.btn_download.setEnabled(True)
        self.btn_check_again.setEnabled(True)
        self.lbl_status.setText("✅ 安装包已解压准备就绪！")

        if getattr(sys, 'frozen', False):
            dst_dir = os.path.dirname(sys.executable)
            exe_name = os.path.basename(sys.executable)
        else:
            dst_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            exe_name = "Ha-Gemi-Dynamic.exe" if self.is_dynamic else "Ha-Gemi.exe"

        action_text = "覆盖升级" if self._is_newer else "重新安装"
        title = "自动更新覆盖就绪" if self._is_newer else "重新安装覆盖就绪"

        reply = QMessageBox.question(
            self,
            title,
            f"安装包已成功下载并解压就绪！✨\n\n"
            f"点击【确定】后，哈Gemi 将自动退出并执行{action_text}，完成后会自动重新启动！\n"
            "（您的个人配置 config.json 和记忆文件 memory.json 将被完好保留）\n\n"
            f"是否立即{action_text}并重启？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if reply == QMessageBox.StandardButton.Yes:
            success = launch_auto_installer(src_dir, dst_dir, exe_name)
            if success:
                QApplication.quit()
                os._exit(0)
            else:
                QMessageBox.warning(self, "更新失败", "启动自动更新安装程序失败，请尝试手动解压更新。")

    def _on_extract_error(self, err_msg: str):
        self.btn_auto_update.setEnabled(True)
        self.btn_download.setEnabled(True)
        self.btn_check_again.setEnabled(True)
        self.lbl_status.setText(f"❌ 解压安装包失败: {err_msg}")
        QMessageBox.warning(self, "解压失败", f"解压新版本压缩包时出错: {err_msg}\n建议前往 GitHub 手动下载。")

    def _on_download_error(self, err_msg: str):
        self.progress_bar.setVisible(False)
        self.btn_auto_update.setEnabled(True)
        self.btn_download.setEnabled(True)
        self.btn_check_again.setEnabled(True)
        self.lbl_status.setText(f"❌ 下载中断: {err_msg}")
        QMessageBox.warning(self, "下载失败", f"无法直接下载该资产文件: {err_msg}\n\n建议点击【前往 GitHub 发布页】在浏览器中直接下载。")
