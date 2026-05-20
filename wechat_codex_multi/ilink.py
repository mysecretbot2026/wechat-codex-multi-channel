ILINK_APP_ID = "bot"

# Tencent @tencent-weixin/openclaw-weixin 2.4.3 encodes this as
# major << 16 | minor << 8 | patch: 0x00020403.
ILINK_APP_CLIENT_VERSION = "132099"


def ilink_common_headers():
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
