TERMINAL_PROTOCOL = "pi-terminal.v1"
TERMINAL_TICKET_PREFIX = "pi-terminal-ticket."
MAX_TERMINAL_FRAME_BYTES = 65_536
TERMINAL_WARNINGS = [
    "Terminal 拥有整个 sandbox 的文件权限，不受 cwd 限制。",
    "Agent Turn 与 Terminal 可以并发修改 workspace，调用方必须处理冲突。",
]
