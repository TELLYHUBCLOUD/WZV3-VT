class MegaSdkUnavailable(RuntimeError):
    pass


try:
    from mega import (  # type: ignore
        MegaApi,
        MegaCancelToken,
        MegaError,
        MegaListener,
        MegaRequest,
        MegaStringList,
        MegaTransfer,
        MegaUploadOptions,
    )

    MEGA_SDK_AVAILABLE = True
    MEGA_SDK_IMPORT_ERROR = None
except Exception as e:
    MEGA_SDK_AVAILABLE = False
    MEGA_SDK_IMPORT_ERROR = e

    class _MissingMegaApi:
        def __init__(self, *args, **kwargs):
            raise MegaSdkUnavailable(
                "Mega SDK Python bindings are not installed. Disable Mega features "
                "or install the official MEGA SDK bindings that provide "
                "`from mega import MegaApi`."
            ) from MEGA_SDK_IMPORT_ERROR

    class _MissingMegaCancelToken:
        @staticmethod
        def createInstance():
            return None

    class _MissingMegaError:
        API_OK = 0
        API_EAGAIN = -3
        API_ERATELIMIT = -4
        API_EINCOMPLETE = -13
        API_EOVERQUOTA = -17

    class _MissingMegaRequest:
        TYPE_LOGIN = 1
        TYPE_FETCH_NODES = 2
        TYPE_GET_PUBLIC_NODE = 3
        TYPE_LOGOUT = 4
        TYPE_ACCOUNT_DETAILS = 5
        TYPE_EXPORT = 6
        TYPE_CREATE_FOLDER = 7
        TYPE_IMPORT_LINK = 8

    class _MissingMegaTransfer:
        TYPE_DOWNLOAD = 1
        TYPE_UPLOAD = 2

    class _MissingMegaUploadOptions:
        pass

    class _MissingMegaStringList:
        @staticmethod
        def createInstance():
            return None

    class _MissingMegaListener:
        pass

    MegaApi = _MissingMegaApi
    MegaCancelToken = _MissingMegaCancelToken
    MegaError = _MissingMegaError
    MegaListener = _MissingMegaListener
    MegaRequest = _MissingMegaRequest
    MegaStringList = _MissingMegaStringList
    MegaTransfer = _MissingMegaTransfer
    MegaUploadOptions = _MissingMegaUploadOptions


def ensure_mega_sdk():
    if not MEGA_SDK_AVAILABLE:
        raise MegaSdkUnavailable(
            "Mega SDK Python bindings are not installed. Mega download, upload, "
            "clone, and account-info features are unavailable in this container. "
            "Disable Mega features or install the official MEGA SDK bindings that "
            "provide `from mega import MegaApi`."
        ) from MEGA_SDK_IMPORT_ERROR
