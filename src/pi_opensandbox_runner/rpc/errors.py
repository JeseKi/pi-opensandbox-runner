class RpcError(RuntimeError):
    pass


class RpcProcessExited(RpcError):
    pass


class SessionCapacityExceeded(RpcError):
    pass
