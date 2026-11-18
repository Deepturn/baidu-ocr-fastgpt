class ServiceError(Exception):
    def __init__(
        self, code: str, status_text: str = "pdfParseFailed", http_status: int = 200
    ):
        super().__init__(code)
        self.code = code
        self.status_text = status_text
        self.http_status = http_status

    def payload(self) -> dict:
        return {
            "success": False,
            "markdown": "",
            "pages": 0,
            "error": self.code,
            "statusText": self.status_text,
        }
