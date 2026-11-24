class ProtocolError(Exception):
    """Safe machine code only; never attach native error text or request data."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status
