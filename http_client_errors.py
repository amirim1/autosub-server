class HttpClientError(RuntimeError):
    """Safe base error for managed upstream HTTP operations."""


class HttpClientNotStartedError(HttpClientError):
    pass


class UnsafePanelUrlError(HttpClientError):
    pass


class UpstreamConnectionError(HttpClientError):
    pass


class UpstreamTimeoutError(HttpClientError):
    pass


class UpstreamTlsError(HttpClientError):
    pass


class UpstreamAuthenticationError(HttpClientError):
    pass


class UpstreamResponseError(HttpClientError):
    pass


class UpstreamServerError(UpstreamResponseError):
    pass


class UpstreamResponseTooLargeError(UpstreamResponseError):
    pass
