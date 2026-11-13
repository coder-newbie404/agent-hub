"""Symbol tables for the Discord UI: status dots, embed accent colors, and
activity/action icons. Single source of truth so every surface renders alike."""

STATUS_ICON = {"created": "⚪", "waiting": "🟢", "running": "🟡",
               "completed": "🟢", "failed": "🔴", "cancelled": "🟣"}
STATUS_COLOR = {"created": 0x9E9E9E, "waiting": 0x2ECC71, "running": 0xF1C40F,
                "completed": 0x2ECC71, "failed": 0xE74C3C,
                "cancelled": 0x9B59B6}
AGENT_STATUS_ICON = {"online": "🟢", "offline": "⚪"}
ARCHIVED_ICON = "⚫"
# Capabilities worth rendering an explicit ✗ for when the agent lacks them
# (roadmap §11: do not expose unsupported controls).
NOTABLE_CAPS = ("interrupt", "inject_input", "approval")

# Action icons used on buttons across surfaces.
STOP = "🛑"
RENAME = "✏️"
ARCHIVE = "📦"
DELETE = "🗑️"
CONFIRM = "✅"
CANCEL = "✖"
OPEN_THREAD = "🧭"
NEW_THREAD = "🧵"
RESUME = "▶️"

# keyword → icon, matched against session.activity labels ("tool: detail").
_ACTIVITY_KEYWORDS = (("grep", "🔍"), ("search", "🔍"), ("find", "🔍"),
                      ("read", "📄"), ("file", "📄"), ("bash", "🔧"),
                      ("terminal", "🔧"), ("command", "🔧"), ("test", "⏳"),
                      ("web", "🌐"), ("fetch", "🌐"))


def status_icon(status: str) -> str:
    return STATUS_ICON.get(status, "⚪")


def activity_icon(label: str | None) -> str:
    if not label:
        return "⏳"
    lowered = label.lower()
    if "think" in lowered:
        return "🧠"
    for key, icon in _ACTIVITY_KEYWORDS:
        if key in lowered:
            return icon
    return "🔧"
