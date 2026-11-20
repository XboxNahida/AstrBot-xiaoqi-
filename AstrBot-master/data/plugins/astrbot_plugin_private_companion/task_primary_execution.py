"""主要用户的本机管理执行；身份由宿主实时核对，不接受工具参数授权。"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from .task_execution import (
        ExecutionIdentity,
        _host_acl_probe,
        _NativeDuplexChannel,
        _NativeProcess,
        actor_workspace,
    )
except ImportError:
    from task_execution import (
        ExecutionIdentity,
        _host_acl_probe,
        _NativeDuplexChannel,
        _NativeProcess,
        actor_workspace,
    )


class PrimaryDeploymentVerifier:
    """每次执行重查当前主要用户绑定及 SYSTEM 宿主。

    Args:
        authorize: 宿主提供的实时主要用户身份检查。
        task_root: 宿主持有的任务工作区根目录。
        broker_root: 仅管理员和 SYSTEM 可访问的部署目录。
    """

    def __init__(self, *, authorize, task_root, broker_root):
        self.authorize = authorize
        self.task_root = Path(task_root)
        self.broker_root = Path(broker_root)

    def verify(self, actor, workspace):
        """返回已验证的宿主身份，撤销主要用户身份后立即拒绝。

        Args:
            actor: 来自真实平台事件的宿主身份。
            workspace: 该身份的默认任务产物目录。

        Returns:
            SYSTEM 执行身份。

        Raises:
            PermissionError: 身份、工作区或宿主权限不匹配。
        """
        if (
            os.name != "nt"
            or self.authorize(actor) is not True
            or Path(workspace).resolve()
            != actor_workspace(self.task_root, actor).resolve()
        ):
            raise PermissionError("Current primary-user authority required")
        _host_acl_probe(self.broker_root, ())
        return ExecutionIdentity("NT AUTHORITY", "SYSTEM", "S-1-5-18")


class WindowsPrimaryBackend:
    """使用宿主令牌启动进程，沿用 Job 取消、超时和句柄隔离机制。"""

    def start(self, identity, argv, cwd, env):
        """启动已通过实时身份检查的管理命令。

        Args:
            identity: 主要用户校验器返回的固定 SYSTEM 身份。
            argv: 可执行文件及参数。
            cwd: 宿主确认的默认工作目录。
            env: 显式子进程环境，不继承机器人密钥。

        Returns:
            拥有整个子孙进程 Job 的进程对象。

        Raises:
            PermissionError: 不是 Windows 或固定宿主身份。
        """
        if os.name != "nt" or identity != ExecutionIdentity(
            "NT AUTHORITY", "SYSTEM", "S-1-5-18"
        ):
            raise PermissionError("Host execution identity mismatch")
        return _NativeProcess(identity, "", "", None, argv, cwd, env, host_token=True)

    def start_duplex(self, identity, argv, cwd, env):
        """启动管理身份的双向通道，供主要用户的 Codex 任务使用。

        Args:
            identity: 已验证的宿主身份。
            argv: 可执行文件及参数。
            cwd: 默认工作目录。
            env: 显式子进程环境。

        Returns:
            可取消并回收子孙进程的双向通道。

        Raises:
            PermissionError: 执行身份不匹配。
        """
        if os.name != "nt" or identity != ExecutionIdentity(
            "NT AUTHORITY", "SYSTEM", "S-1-5-18"
        ):
            raise PermissionError("Host execution identity mismatch")
        process = _NativeProcess(
            identity, "", "", None, argv, cwd, env, duplex=True, host_token=True
        )
        try:
            return _NativeDuplexChannel(process)
        except BaseException:
            process.close()
            raise
