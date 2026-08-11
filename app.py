"""
Bookly Customer Support Agent — Streamlit Web UI (v2)
--------------------------------------------------------
New in this version:
- Preset return reasons (dropdown in the sidebar form, and constrained via
  tool schema `enum` so free-text chat stays consistent too)
- "Good friction" save flows: before finalizing a return, the agent checks
  the reason + order contents and may recommend a free reshipment (damaged
  item) or a same-genre swap (changed mind) instead of a plain refund
- Orders now carry a genre per item, and there's a small mock catalog used
  for swap recommendations

Run:
    pip3 install anthropic streamlit
    export ANTHROPIC_API_KEY=sk-...
    python3 -m streamlit run app.py
"""

import json
import streamlit as st
from anthropic import Anthropic

client = Anthropic()  # reads ANTHROPIC_API_KEY from env
MODEL = "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# 1. MOCK DATA
#    Swap this section out for a Google Sheets read once that's wired up —
#    everything below just needs to end up looking like MOCK_ORDERS.
# ---------------------------------------------------------------------------

MOCK_ORDERS = {
    "BK1001": {
        "email": "jane.doe@example.com",
        "status": "Delivered",
        "eta": "Aug 8",
        "items": [{"title": "The Hobbit", "genre": "Fantasy"}],
        "tracking_url": "https://bookly.fakeurl/track/BK1001",
    },
    "BK1002": {
        "email": "sam.lee@example.com",
        "status": "Delivered",
        "eta": "Aug 4",
        "items": [
            {"title": "Dune", "genre": "Science Fiction"},
            {"title": "Project Hail Mary", "genre": "Science Fiction"},
        ],
        "tracking_url": "https://bookly.fakeurl/track/BK1002",
    },
    "BK1003": {
        "email": "alex.kim@example.com",
        "status": "Processing",
        "eta": "Aug 13",
        "items": [{"title": "Harry Potter and the Philosopher's Stone", "genre": "Fantasy"}],
        "tracking_url": "https://bookly.fakeurl/track/BK1003",
    },
    "BK1004": {
        "email": "morgan.reyes@example.com",
        "status": "In Transit",
        "eta": "Aug 15",
        "items": [{"title": "Gone Girl", "genre": "Mystery"}],
        "tracking_url": "https://bookly.fakeurl/track/BK1004",
    },
}

# Preset return reasons — used both as the dropdown options in the UI and as
# an `enum` constraint on the tool schema, so free-text chat stays consistent.
RETURN_REASONS = [
    "Select a reason",
    "Item arrived damaged",
    "Wrong item received",
    "Changed my mind",
    "Other",
]

# Small mock catalog used for swap recommendations, keyed by genre
CATALOG = {
    "Science Fiction": ["I, Robot", "Foundation", "The Martian"],
    "Fantasy": ["The Lord of the Rings", "American Gods", "The Way of Kings", "Narnia"],
    "Mystery": ["Gone Girl", "The Hound of the Baskervilles", "The Silent Patient"],
}

# ---------------------------------------------------------------------------
# 2. MOCKED BACKEND FUNCTIONS
# ---------------------------------------------------------------------------

def _verify_identity(order_id: str, email: str):
    order = MOCK_ORDERS.get(order_id.upper())
    if not order:
        return {"error": f"No order found with ID {order_id}"}
    if order["email"].lower() != email.strip().lower():
        return {
            "error": "identity_mismatch",
            "message": (
                "The email provided does not match the email on file for this order. "
                "Ask the customer to double check the email associated with their Bookly account."
            ),
        }
    return None


def get_order_status(order_id: str, email: str) -> dict:
    mismatch = _verify_identity(order_id, email)
    if mismatch:
        return mismatch
    order = MOCK_ORDERS[order_id.upper()]
    return {
        "order_id": order_id.upper(),
        "status": order["status"],
        "eta": order["eta"],
        "items": [i["title"] for i in order["items"]],
        "tracking_url": order["tracking_url"],
    }


def get_return_recommendation(order_id: str, email: str, reason: str) -> dict:
    """'Good friction' step: decide whether a plain refund is really the best
    outcome, or whether a free reshipment / same-genre swap serves the
    customer (and Bookly) better. Does NOT finalize anything yet."""
    mismatch = _verify_identity(order_id, email)
    if mismatch:
        return mismatch

    order = MOCK_ORDERS[order_id.upper()]

    # Gate 1: can't return/refund something that hasn't been delivered yet,
    # regardless of the reason given. Surface the tracking link instead.
    if order["status"] != "Delivered":
        return {
            "recommended_action": "wait_for_delivery",
            "current_status": order["status"],
            "eta": order["eta"],
            "tracking_url": order["tracking_url"],
            "details": (
                "Order has not been delivered yet, so no return or refund can be processed. "
                "Share the tracking link and ETA, and let the customer know they can request "
                "a return once the order arrives."
            ),
        }

    titles_in_order = [i["title"] for i in order["items"]]
    genres_in_order = {i["genre"] for i in order["items"]}

    if reason in ("Item arrived damaged", "Item defective / not working"):
        return {
            "recommended_action": "reship_replacement",
            "details": (
                "Item issue is on Bookly's side, not the customer's preference. "
                "Recommend shipping a free brand-new replacement copy instead of a refund."
            ),
        }

    if reason in ("Changed my mind", "No longer needed"):
        # find an alternative title in the same genre, not already in the order
        alternative = None
        for genre in genres_in_order:
            for candidate in CATALOG.get(genre, []):
                if candidate not in titles_in_order:
                    alternative = {"title": candidate, "genre": genre}
                    break
            if alternative:
                break

        if alternative:
            genre_slug = alternative["genre"].lower().replace(" ", "-")
            return {
                "recommended_action": "swap_alternative",
                "alternative_title": alternative["title"],
                "alternative_genre": alternative["genre"],
                "catalog_url": f"https://bookly.fake/catalog/{genre_slug}",
                "details": (
                    f"Customer's order contains {alternative['genre']} titles. Recommend "
                    f"swapping the returned item for '{alternative['title']}' (same genre) "
                    "with free shipping, instead of a refund. Also mention the catalog link "
                    "in case they'd rather pick something else in the same genre themselves."
                ),
            }

    # Fallback: no good alternative applies (wrong item, other, or no catalog match)
    return {
        "recommended_action": "standard_return",
        "details": "No better alternative applies here — proceed with a standard refund return.",
    }


def initiate_return(order_id: str, email: str, reason: str, resolution: str, alternative_title: str = None) -> dict:
    """Finalizes the return. `resolution` should be one of:
    'refund', 'reship_replacement', 'swap_alternative' — normally decided
    after the customer responds to get_return_recommendation's suggestion."""
    mismatch = _verify_identity(order_id, email)
    if mismatch:
        return mismatch

    order = MOCK_ORDERS[order_id.upper()]

    # Defensive guard — mirrors the check in get_return_recommendation. Even
    # if this is called out of order, undelivered orders can't be finalized.
    if order["status"] != "Delivered":
        return {
            "error": "order_not_delivered",
            "current_status": order["status"],
            "tracking_url": order["tracking_url"],
            "message": "This order hasn't been delivered yet, so no return can be finalized. Share the tracking link instead.",
        }

    base = {"order_id": order_id.upper(), "reason": reason, "resolution": resolution}

    if resolution == "reship_replacement":
        return {**base, "status": "replacement_shipped", "eta": "3-5 business days", "cost_to_customer": "£0.00"}

    if resolution == "swap_alternative":
        return {
            **base,
            "status": "swap_processed",
            "new_title": alternative_title,
            "eta": "3-5 business days",
            "cost_to_customer": "£0.00",
        }

    # default: standard refund return
    return {
        **base,
        "status": "return_initiated",
        "refund_eta_days": 5,
        "return_label_url": "https://bookly.fake/labels/RTN-88213.pdf",
    }


def reset_password_link(email: str) -> dict:
    return {
        "email": email,
        "reset_link": f"https://bookly.fake/reset?token=abc123-{hash(email) % 9999}",
        "expires_in_minutes": 30,
    }

def flag_return_intent() -> dict:
    """No-op — this tool exists purely as a signal. Calling it tells the app
    'the customer wants to return something,' nothing more."""
    return {
        "acknowledged": True
    }


TOOL_IMPL = {
    "get_order_status": get_order_status,
    "get_return_recommendation": get_return_recommendation,
    "initiate_return": initiate_return,
    "reset_password_link": reset_password_link,
    "flag_return_intent": flag_return_intent, 
}


# ---------------------------------------------------------------------------
# 3. TOOL SCHEMAS
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_order_status",
        "description": "Look up the current status, ETA, and contents of a Bookly order. Requires order ID and account email for identity verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Bookly order ID, e.g. BK1234"},
                "email": {"type": "string", "description": "Email on the customer's Bookly account, used for identity verification"},
            },
            "required": ["order_id", "email"],
        },
    },
    {
        "name": "get_return_recommendation",
        "description": (
            "Call this FIRST whenever a customer wants to return an item, before initiate_return. "
            "Checks the reason and the order's contents to see if a free reshipment or a same-genre "
            "swap would serve the customer better than a plain refund. Does not finalize anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Bookly order ID, e.g. BK1234"},
                "email": {"type": "string", "description": "Email on the customer's Bookly account, used for identity verification"},
                "reason": {"type": "string", "enum": RETURN_REASONS, "description": "Reason for the return, must match one of the preset options"},
            },
            "required": ["order_id", "email", "reason"],
        },
    },
    {
        "name": "initiate_return",
        "description": (
            "Finalizes a return AFTER get_return_recommendation has been called and the customer "
            "has confirmed how they'd like to proceed (plain refund, free reshipment, or swap)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Bookly order ID, e.g. BK1234"},
                "email": {"type": "string", "description": "Email on the customer's Bookly account, used for identity verification"},
                "reason": {"type": "string", "enum": RETURN_REASONS, "description": "Reason for the return"},
                "resolution": {
                    "type": "string",
                    "enum": ["refund", "reship_replacement", "swap_alternative"],
                    "description": "How the customer wants to resolve this, confirmed after seeing the recommendation",
                },
                "alternative_title": {
                    "type": "string",
                    "description": "Required only when resolution is swap_alternative — the title being swapped in",
                },
            },
            "required": ["order_id", "email", "reason", "resolution"],
        },
    },
    {
        "name": "reset_password_link",
        "description": "Generate a password reset link for a customer's Bookly account.",
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string", "description": "Customer's account email"}},
            "required": ["email"],
        },
    },
    {
        "name": "flag_return_intent",
        "description": (
            "Call this the moment you detect the customer wants to start a return, "
            "refund, or exchange — even before you have any other details from them. "
            "This does not finalize anything, it just signals intent so the app can "
            "show a details form. After calling it, give a short warm acknowledgment "
            "and STOP — do not ask for order ID, email, or reason yourself; a form will "
            "collect those. Do not call this again if a return is already in progress."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# 4. SYSTEM PROMPT
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are Bookly Support, the customer support agent for Bookly, an online bookstore.

STORE POLICIES (answer general questions directly from this, no tool needed):
- Standard shipping: 5-7 business days, free over £25. Expedited: 2-3 days, £2.99.
- Returns: accepted within 30 days of delivery, items must be unused. Refunds issued to
  original payment method within 5 business days of the return being received.
- Password resets are self-serve via a reset link sent to the account email.

PRESET RETURN REASONS (only use these, don't invent new ones): {", ".join(RETURN_REASONS)}

RETURN FLOW (important — follow this order):
1. Get the order ID, account email, and return reason from the customer (ask if missing).
2. Call get_return_recommendation FIRST. Never call initiate_return before this.
3. If recommended_action is "wait_for_delivery": the order hasn't arrived yet, so no return
   or refund is possible right now. Let the customer know kindly, share the tracking link and
   ETA, and tell them they're welcome to request a return once it arrives. Do not proceed to
   initiate_return in this case.
4. Otherwise, based on the recommendation:
   - If recommended_action is "reship_replacement": warmly offer a free replacement copy
     instead of a refund (e.g. "Since it arrived damaged, we'd like to send you a brand new
     copy at no cost instead of a refund — would that work?"). Only fall back to a plain
     refund if the customer declines.
   - If recommended_action is "swap_alternative": suggest the specific alternative title by
     name, and mention the catalog_url as an option too, framed as a friendly offer, e.g.
     "Would you like to swap it for 'The Name of the Wind', or pick anything else from
     bookly.fake/catalog/fantasy? It's the same genre as what you ordered, and we'll cover
     shipping at no cost." Only proceed with a plain refund if they decline.
   - If recommended_action is "standard_return": just proceed with a normal refund return.
5. Once the customer confirms which resolution they want, call initiate_return with that
   resolution ("refund", "reship_replacement", or "swap_alternative", plus alternative_title
   if it's a swap).

GENERAL BEHAVIOR:
- Never fabricate order details, statuses, or confirmation numbers — always use tools.
- If required info is missing, ASK a clarifying question rather than guessing.
- SECURITY: order lookups and returns require both order ID and account email. If a tool
  returns "identity_mismatch", do not reveal any details — ask the customer to double check
  their email. Only share tracking links or order details once identity is verified.
- When a get_order_status result includes a tracking_url and the order hasn't been delivered
  yet, mention the tracking link so the customer can follow its progress.
- Keep responses concise, warm, and professional.
- If a request has nothing to do with Bookly orders/returns/account/policy, politely say so.
"""


# ---------------------------------------------------------------------------
# 5. AGENT LOOP
# ---------------------------------------------------------------------------

def run_turn(messages: list) -> list:
    response = client.messages.create(
        model=MODEL, max_tokens=1024, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages
    )
    messages.append({"role": "assistant", "content": response.content})

    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                st.session_state.tool_log.append(f"🔧 {block.name}({block.input})")
                fn = TOOL_IMPL.get(block.name)
                result = fn(**block.input) if fn else {"error": "unknown tool"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model=MODEL, max_tokens=1024, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

    return messages


def extract_text(messages: list) -> str:
    last = messages[-1]
    if last["role"] != "assistant":
        return ""
    return "\n".join(_block_text(b) for b in last["content"] if _block_type(b) == "text")


def _block_type(block) -> str:
    """Works whether `block` is a real Anthropic SDK content object (attribute
    access) or a plain dict (used for our own locally-generated messages)."""
    return block["type"] if isinstance(block, dict) else block.type


def _block_text(block) -> str:
    """Same idea as _block_type, for pulling out the text field."""
    return block.get("text", "") if isinstance(block, dict) else block.text


def _finalized_return_since(tool_log_length_before: int) -> bool:
    """Checks whether initiate_return actually fired during the tool calls
    logged since the given point — i.e. the return flow is truly done."""
    new_entries = st.session_state.tool_log[tool_log_length_before:]
    return any("initiate_return(" in entry for entry in new_entries)

def _flagged_return_intent_since(tool_log_length_before: int) -> bool:
    """Checks whether Claude called flag_return_intent during the tool calls
    logged since the given point."""
    new_entries = st.session_state.tool_log[tool_log_length_before:]
    return any("flag_return_intent(" in entry for entry in new_entries)


# ---------------------------------------------------------------------------
# 6. STREAMLIT UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Bookly Support", page_icon="📚")
st.title("📚 Bookly Support 🤓")
st.caption("How can I help today? Tell me what's going on, and I'll do my best to help.")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "tool_log" not in st.session_state:
    st.session_state.tool_log = []
if "awaiting_return_details" not in st.session_state:
    st.session_state.awaiting_return_details = False
if "return_flow_active" not in st.session_state:
    # True from the moment a return is first raised until initiate_return
    # actually finalizes it. Prevents "refund"/"return" showing up in a later
    # confirmation reply from re-triggering the inline form mid-flow.
    st.session_state.return_flow_active = False

import re

_ORDER_ID_RE = re.compile(r"\bBK\d{3,6}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _known_order_id() -> str:
    """Scan prior user turns for anything that looks like an order ID, so the
    inline form can be pre-filled instead of asking again."""
    for msg in reversed(st.session_state.messages):
        if msg["role"] == "user" and isinstance(msg["content"], str):
            match = _ORDER_ID_RE.search(msg["content"])
            if match:
                return match.group(0).upper()
    return ""


def _known_email() -> str:
    """Scan prior user turns for anything that looks like an email, so the
    inline form can be pre-filled instead of asking again."""
    for msg in reversed(st.session_state.messages):
        if msg["role"] == "user" and isinstance(msg["content"], str):
            match = _EMAIL_RE.search(msg["content"])
            if match:
                return match.group(0)
    return ""

with st.sidebar:
    st.subheader("Demo accounts")
    st.markdown(
        "- **BK1001** — jane.doe@example.com (Fantasy, Delivered)\n"
        "- **BK1002** — sam.lee@example.com (Sci-Fi x2, Delivered)\n"
        "- **BK1003** — alex.kim@example.com (Fantasy, Processing)\n"
        "- **BK1004** — morgan.reyes@example.com (Mystery, In Transit — try requesting a return on this one)"
    )

    if st.session_state.tool_log:
        st.subheader("Tool calls this session")
        for entry in st.session_state.tool_log:
            st.code(entry, language=None)

    if st.button("Reset conversation"):
        st.session_state.messages = []
        st.session_state.tool_log = []
        st.session_state.awaiting_return_details = False
        st.session_state.return_flow_active = False
        st.rerun()

# Render chat history
for msg in st.session_state.messages:
    if msg["role"] == "user" and isinstance(msg["content"], str):
        with st.chat_message("user"):
            st.write(msg["content"])
    elif msg["role"] == "assistant":
        text = "\n".join(_block_text(b) for b in msg["content"] if _block_type(b) == "text")
        if text:
            with st.chat_message("assistant"):
                st.write(text)

# If we're mid-return-flow, show the detail collector inline, right where the
# next assistant reply would normally go, instead of a sidebar widget.
if st.session_state.awaiting_return_details:
    known_order_id = _known_order_id()
    known_email = _known_email()
    with st.chat_message("assistant"):
        st.write("Sure — I can help with that. Just need a few details:")
        with st.form("inline_return_form"):
            r_order_id = st.text_input("Order ID", value=known_order_id, placeholder="BK1234")
            r_email = st.text_input("Account email", value=known_email)
            r_reason = st.selectbox("Reason for return", RETURN_REASONS)
            submitted = st.form_submit_button("Continue")

        if submitted and r_order_id and r_email:
            synthetic_message = (
                f"I'd like to return order {r_order_id.strip()} "
                f"(my account email is {r_email.strip()}). Reason: {r_reason}"
            )
            st.session_state.awaiting_return_details = False
            st.session_state.return_flow_active = True
            st.session_state.messages.append({"role": "user", "content": synthetic_message})

            with st.chat_message("user"):
                st.write(synthetic_message)
            log_len_before = len(st.session_state.tool_log)
            with st.spinner("Thinking..."):
                st.session_state.messages = run_turn(st.session_state.messages)
            if _finalized_return_since(log_len_before):
                st.session_state.return_flow_active = False
            reply_text = extract_text(st.session_state.messages)
            with st.chat_message("assistant"):
                st.write(reply_text)

# Normal chat input — skipped while the inline form above is active
user_input = None if st.session_state.awaiting_return_details else st.chat_input("Type your message...")

if user_input:
    with st.chat_message("user"):
        st.write(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})

    log_len_before = len(st.session_state.tool_log)
    with st.spinner("Thinking..."):
        st.session_state.messages = run_turn(st.session_state.messages)

    if _flagged_return_intent_since(log_len_before) and not st.session_state.return_flow_active:
        # Claude itself recognized return intent — show the form now
        st.session_state.return_flow_active = True
        st.session_state.awaiting_return_details = True
        st.rerun()
    else:
        if _finalized_return_since(log_len_before):
            st.session_state.return_flow_active = False
        reply_text = extract_text(st.session_state.messages)
        with st.chat_message("assistant"):
            st.write(reply_text)
