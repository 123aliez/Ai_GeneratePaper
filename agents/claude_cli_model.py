"""Claude Code 订阅适配器：无工具、无持久会话的纯内容调用。"""
from __future__ import annotations

import errno
import json
import os
import re
import signal
import struct
import subprocess
import tempfile
import time

from smolagents.models import ChatMessage, ChatMessageStreamDelta, MessageRole, Model, TokenUsage

from .retry_policy import ModelCallError


CLI_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
DIAGNOSTIC_TAIL_CHARS = 4000
PROCESS_REAP_TIMEOUT = 0.2
MAX_ARGUMENT_BYTES = 128 * 1024
TRANSCRIPT_HEADER = (
    "The following is a conversation transcript. Earlier turns are history; "
    "reply only to the last USER turn."
)


class ClaudeCliError(ModelCallError):
    """CLI 的失败；诊断尾部保留在异常属性中，不直接打印材料或服务端原文。"""

    def __init__(self, message: str, *, retryable: bool = False,
                 status_code: int | None = None, stdout="", stderr=""):
        super().__init__(message, retryable=retryable, status_code=status_code)
        self.stdout_tail = self._tail(stdout)
        self.stderr_tail = self._tail(stderr)

    @staticmethod
    def _tail(value) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return (value or "")[-DIAGNOSTIC_TAIL_CHARS:]


class ClaudeSubscriptionQuotaExceeded(ClaudeCliError):
    """订阅额度耗尽：即使状态为 429，也禁止模型层和阶段层重试。"""

    def __init__(self, *, owner="Agent", status_code=None, stdout="", stderr=""):
        prefix = {"Draft": "DRAFT", "Review": "REVIEW", "Manager": "MANAGER"}.get(owner, "*")
        super().__init__(
            "Claude 订阅额度已用尽。请等额度恢复后重跑同一条命令；"
            f"或把 .env 中对应的 {prefix}_PROVIDER 改为 openai/anthropic，"
            "并配置该组 MODEL/API_KEY/API_BASE 后重跑（已完成的内容会续上）。",
            status_code=status_code, stdout=stdout, stderr=stderr,
        )


class ClaudeCliModel(Model):
    """构造时只保存配置；子进程、临时目录都延迟到 generate。"""

    def __init__(self, model_id: str, effort: str | None = None,
                 timeout: float = 600.0, cli_path: str | None = None, owner="Agent"):
        if effort is not None and effort not in CLI_EFFORTS:
            raise ValueError(f"claude_cli 的 effort 仅支持 {sorted(CLI_EFFORTS)}，收到 {effort!r}")
        super().__init__(model_id=model_id)
        self.effort = effort
        self.timeout = timeout
        self.cli_path = cli_path or os.getenv("CLAUDE_CLI_PATH") or "claude"
        self.owner = owner

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") in (None, "text"):
                    parts.append(str(part.get("text", "")))
            return "\n".join(parts)
        return ""

    def _messages(self, messages) -> tuple[str, str]:
        systems, turns = [], []
        for message in messages:
            role = message.get("role") if isinstance(message, dict) else message.role
            role = role.value if hasattr(role, "value") else role
            content = message.get("content") if isinstance(message, dict) else message.content
            text = self._text(content)
            if role == "system":
                systems.append(text)
            elif role in ("user", "assistant"):
                turns.append((role, text))
            else:
                raise ClaudeCliError("claude_cli 仅支持 system/user/assistant 文本消息。")
        if not turns or turns[-1][0] != "user":
            raise ClaudeCliError("claude_cli 要求对话以 user 消息结束。")
        if len(turns) == 1:
            prompt = turns[0][1]
        else:
            prompt = TRANSCRIPT_HEADER + "\n\n" + "\n\n".join(
                f'<turn role="{role}">\n{text}\n</turn>' for role, text in turns)
        return "\n\n".join(systems), prompt

    @staticmethod
    def _argument_size_error():
        return ClaudeCliError(
            f"Claude CLI 参数长度超限：每项参数（含结尾空字节）最多 {MAX_ARGUMENT_BYTES} 字节，"
            "参数与环境总量还受系统 ARG_MAX 限制。请缩短 system 提示词或相关参数/环境变量。")

    def _check_argument_size(self, args, env):
        # Linux 对单个参数还有 MAX_ARG_STRLEN 限制，不能仅检查参数总长。
        sizes = [len(os.fsencode(arg)) + 1 for arg in args]
        env_sizes = [len(os.fsencode(f"{key}={value}")) + 1 for key, value in env.items()]
        try:
            total_limit = os.sysconf("SC_ARG_MAX")
        except (AttributeError, OSError, ValueError):
            total_limit = 2 * 1024 * 1024
        if total_limit <= 0:
            total_limit = 2 * 1024 * 1024
        pointer_bytes = (len(args) + len(env) + 2) * struct.calcsize("P")
        if any(size > MAX_ARGUMENT_BYTES for size in sizes + env_sizes) or \
                sum(sizes + env_sizes) + pointer_bytes > total_limit:
            raise self._argument_size_error()

    @staticmethod
    def _terminate_group(process, stdout=None, stderr=None):
        """终止整个会话进程组；管道排空和直接子进程回收共用一个有限期限。"""
        deadline = time.monotonic() + PROCESS_REAP_TIMEOUT
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # 进程组信号不可用时仍尝试终止直接子进程，后续等待依然有上限。
            try:
                process.kill()
            except OSError:
                pass
        try:
            out, err = process.communicate(timeout=max(0, deadline - time.monotonic()))
            return out or stdout, err or stderr
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.output or stdout, exc.stderr or stderr
        except (OSError, ValueError):
            pass
        finally:
            # 后代意外保留管道时不能再无限排空；只关闭本端，不向 stdin 写入或刷新。
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, name, None)
                if callable(getattr(stream, "close", None)):
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except (subprocess.TimeoutExpired, OSError):
                pass
        return stdout, stderr

    def _failure(self, status, result, stderr, stdout, *, invalid_json=False):
        text = f"{result}\n{stderr}".lower()
        if status == 429 or any(p in text for p in (
                "usage limit", "limit reached", "hit your limit", "you've reached your limit",
                "you have reached your limit")):
            return ClaudeSubscriptionQuotaExceeded(
                owner=self.owner, status_code=status, stdout=stdout, stderr=stderr)
        if status in (401, 403) or re.search(
                r"\b(?:login|log[ -]?in|not logged in|unauthenticated|authentication required)\b", text):
            return ClaudeCliError("Claude 尚未登录或登录已失效，请先在终端运行 claude 完成登录。",
                                  status_code=status, stdout=stdout, stderr=stderr)
        retryable = invalid_json or (status is not None and 500 <= status <= 599) or "overloaded" in text
        if status in (400, 404, 413, 422):
            retryable = False
        return ClaudeCliError(
            "Claude CLI 未返回有效的 JSON 对象。" if invalid_json else
            "Claude CLI 调用失败" + (f"（状态码 {status}）。" if status else "。"),
            retryable=retryable, status_code=status, stdout=stdout, stderr=stderr)

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs) -> ChatMessage:
        for name, value in (("stop_sequences", stop_sequences), ("response_format", response_format),
                            ("tools_to_call_from", tools_to_call_from)):
            if value:
                raise ClaudeCliError(f"claude_cli 不支持非空参数 {name}。")
        if kwargs:
            raise ClaudeCliError("claude_cli 不支持额外生成参数：" + ", ".join(sorted(kwargs)))
        system, prompt = self._messages(messages)
        args = [self.cli_path, "-p", "--output-format", "json", "--tools", "",
                "--safe-mode", "--no-session-persistence", "--model", self.model_id]
        if self.effort is not None:
            args += ["--effort", self.effort]
        if system:
            args += ["--system-prompt", system]
        env = {k: v for k, v in os.environ.items()
               if k != "CLAUDECODE" and not k.startswith("ANTHROPIC_")}
        self._check_argument_size(args, env)
        with tempfile.TemporaryDirectory(prefix="paper-claude-cli-") as cwd:
            try:
                process = subprocess.Popen(
                    args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise ClaudeCliError(
                    f"找不到 Claude CLI 可执行文件 {self.cli_path!r}；请安装 claude 或设置 CLAUDE_CLI_PATH。"
                ) from exc
            except OSError as exc:
                if exc.errno == errno.E2BIG:
                    raise self._argument_size_error() from exc
                raise ClaudeCliError("无法启动 Claude CLI，请检查可执行路径与执行权限。") from exc
            try:
                stdout, stderr = process.communicate(input=prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = self._terminate_group(process, exc.output, exc.stderr)
                raise ClaudeCliError(
                    f"Claude CLI 子进程超时（{self.timeout} 秒），已向整个进程组发送终止信号并限时回收。",
                    retryable=True, stdout=stdout, stderr=stderr,
                ) from exc
            except BaseException:
                # 中断时同样回收子进程，避免残留请求继续消耗额度。
                self._terminate_group(process)
                raise
        try:
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise ValueError("JSON 不是对象")
        except (ValueError, TypeError) as exc:
            # 复用同一套文案识别；额度或登录错误优先，其余无效 JSON 不论退出码均可重试。
            raise self._failure(None, stdout, stderr, stdout, invalid_json=True) from exc
        status = payload.get("api_error_status")
        if isinstance(status, str) and status.isdigit():
            status = int(status)
        if not isinstance(status, int):
            status = None
        result = payload.get("result")
        # subtype 在失败响应中也可能为 success，不能据此接受错误文案为正文。
        if payload.get("is_error") or (status is not None and status >= 400) or process.returncode:
            raise self._failure(status, result, stderr, stdout)
        if payload.get("is_error") is not False or not isinstance(result, str):
            raise ClaudeCliError("Claude CLI JSON 缺少成功标记或文本 result。", stdout=stdout, stderr=stderr)
        usage = payload.get("usage") or {}
        try:
            inputs = sum(int(usage.get(key, 0) or 0) for key in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            outputs = int(usage.get("output_tokens", 0) or 0)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaudeCliError("Claude CLI JSON 的 usage 格式无效。", stdout=stdout, stderr=stderr) from exc
        return ChatMessage(role=MessageRole.ASSISTANT, content=result,
                           token_usage=TokenUsage(input_tokens=inputs, output_tokens=outputs), raw=payload)

    def generate_stream(self, messages, stop_sequences=None, response_format=None,
                        tools_to_call_from=None, **kwargs):
        response = self.generate(messages, stop_sequences=stop_sequences,
                                 response_format=response_format,
                                 tools_to_call_from=tools_to_call_from, **kwargs)
        yield ChatMessageStreamDelta(content=response.content, token_usage=response.token_usage)
