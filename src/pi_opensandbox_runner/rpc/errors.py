class RpcError(RuntimeError):
    pass


class RpcProcessExited(RpcError):
    pass


class McpEnvironmentMissing(RpcError):
    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"missing MCP environment variables: {', '.join(names)}")


class SessionCapacityExceeded(RpcError):
    pass
