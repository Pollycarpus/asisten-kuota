"""
File: core/mock_llm.py
Purpose: Deterministic scripted "LLM" (no network, no key). It reads the masked
         conversation the same way a real model would and emits valid tool calls,
         so all 5 persona scenarios run offline. Used as demo fallback and in tests.
D2 component: Configurable LLM — mock provider (§2.11)
Depends on: core/trace.py
"""

import json
import re
import uuid

from core.trace import estimate_tokens


def tool_call_message(content: str, name: str, arguments: dict) -> dict:
    """
    Build an assistant message that requests one tool call (OpenAI format).

    Why: the mock must produce exactly the shape a real provider returns.
    Args:
        content (str): optional text shown alongside the call.
        name (str): tool name.
        arguments (dict): tool arguments.
    Returns:
        dict: assistant message with tool_calls.
    Raises / Errors:
        None
    Side effects:
        None
    """
    call = {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}
    return {"role": "assistant", "content": content or None, "tool_calls": [call]}


def find_tool_results(messages: list, name: str) -> list:
    """
    Return the parsed results of every earlier call to one tool.

    Why: the script decides its next step from what it has already learned.
    Args:
        messages (list): conversation messages.
        name (str): tool name.
    Returns:
        list: parsed JSON results, oldest first.
    Raises / Errors:
        None
    Side effects:
        None
    """
    results = []
    for message in messages:
        if message.get("role") == "tool" and message.get("name") == name:
            results.append(json.loads(message["content"]))
    return results


def rupiah(amount: int) -> str:
    """
    Format an amount as Rupiah with dot thousands separators (Rp 15.000).

    Why: customer-facing text in Indonesian format.
    Args:
        amount (int): amount in IDR.
    Returns:
        str: formatted amount.
    Raises / Errors:
        None
    Side effects:
        None
    """
    return "Rp " + f"{amount:,}".replace(",", ".")


def after_network(result: dict) -> dict:
    """
    Decide the next step once the diagnosis is known.

    Why: UC1 → UC2 (quota/FUP) or UC5 (network degraded).
    Args:
        result (dict): get_network_status result including "diagnosis".
    Returns:
        dict: next assistant message.
    Raises / Errors:
        None
    Side effects:
        None
    """
    status = (result.get("diagnosis") or {}).get("status")
    if status == "NETWORK_DEGRADED":
        text = ("Kuota dan kecepatan Anda normal, jadi masalahnya bukan di paket. Ada gangguan jaringan di area Anda "
                f"(ref {result.get('incident_ref')}). Saya bisa buatkan laporan gangguan untuk tim jaringan.")
        return tool_call_message(text, "create_ticket", {"symptom": "Internet lambat/tidak stabil di area pelanggan"})
    if status in ("QUOTA_EXHAUSTED", "FUP_THROTTLED"):
        return tool_call_message("", "get_offers", {})
    return {"role": "assistant", "content": "Kuota, kecepatan, dan jaringan Anda terpantau normal. Ada yang bisa saya bantu lagi?"}


def after_offers(result: dict) -> dict:
    """
    Recommend the best affordable offer, or check Paket Darurat when nothing is affordable.

    Why: UC2 (recommend from Top-3 only) and UC4 trigger (price > pulsa).
    Args:
        result (dict): get_offers result.
    Returns:
        dict: next assistant message.
    Raises / Errors:
        None
    Side effects:
        None
    """
    offers = result.get("offers") or []
    if not offers:
        return {"role": "assistant", "content": "Saat ini saya tidak menawarkan paket. Ada yang bisa saya bantu lagi?"}
    affordable = [offer for offer in offers if offer["affordable"]]
    if not affordable:
        cheapest = min(offers, key=lambda offer: offer["price_idr"])
        return tool_call_message("", "check_paket_darurat", {"offer_id": cheapest["offer_id"]})
    pick = affordable[0]
    reason = "pas untuk mengatasi kecepatan yang dibatasi" if pick["category"] == "booster" else "paling hemat dan sesuai kebutuhan Anda"
    text = (f"Saya sarankan {pick['name']} ({pick['validity_days']} hari, {rupiah(pick['price_idr'])}, ID {pick['offer_id']}) "
            f"karena {reason}. Pilihan lain ada di kartu paket. Tekan [Pilih paket ini] bila setuju.")
    return {"role": "assistant", "content": text}


def after_loan(result: dict) -> dict:
    """
    Explain Paket Darurat transparently and request the gated purchase, or offer the fallback.

    Why: UC4 — never hide the fee; never auto-purchase (the purchase call only shows a button).
    Args:
        result (dict): check_paket_darurat result.
    Returns:
        dict: next assistant message.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if result.get("eligible"):
        text = (f"Pulsa Anda belum cukup, tapi Anda bisa memakai Paket Darurat untuk {result['offer_name']} "
                f"({rupiah(result['price_idr'])}). Biaya layanan {rupiah(result['fee_idr'])}, total dibayar "
                f"{rupiah(result['price_idr'] + result['fee_idr'])}, {result['repayment'].lower()}.")
        return tool_call_message(text, "purchase", {"offer_id": result["offer_id"], "payment_method": "PAKET_DARURAT"})
    text = (f"Maaf, Paket Darurat belum tersedia untuk nomor Anda. Anda bisa isi ulang pulsa di {result.get('topup_link')} "
            "lalu membeli paket.")
    return {"role": "assistant", "content": text}


def after_tool(name: str, result: dict) -> dict:
    """
    Choose the next message after a tool result.

    Why: scripted equivalent of the model's reasoning step.
    Args:
        name (str): tool that just returned.
        result (dict): its result.
    Returns:
        dict: next assistant message.
    Raises / Errors:
        None
    Side effects:
        None
    """
    if result.get("error"):
        return {"role": "assistant", "content": f"Maaf, permintaan belum bisa diproses ({result['error']})."}
    if name == "get_quota":
        return tool_call_message("", "get_network_status", {})
    if name == "get_network_status":
        return after_network(result)
    if name == "get_offers":
        return after_offers(result)
    if name == "check_paket_darurat":
        return after_loan(result)
    if name in ("purchase", "create_ticket"):
        return {"role": "assistant", "content": "Silakan periksa detailnya, lalu tekan tombol konfirmasi di kartu."}
    return {"role": "assistant", "content": "Baik. Ada lagi yang bisa saya bantu?"}


def after_user(messages: list, text: str) -> dict:
    """
    Choose the next message after a customer message.

    Why: a chosen package ID → gated purchase; otherwise start the diagnosis.
    Args:
        messages (list): conversation messages.
        text (str): latest customer message.
    Returns:
        dict: next assistant message.
    Raises / Errors:
        None
    Side effects:
        None
    """
    chosen = re.search(r"BID-\d+", text)
    if chosen:
        loans = [r for r in find_tool_results(messages, "check_paket_darurat") if r.get("offer_id") == chosen.group()]
        method = "PAKET_DARURAT" if loans and loans[-1].get("eligible") else "PULSA"
        return tool_call_message("", "purchase", {"offer_id": chosen.group(), "payment_method": method})
    diagnosis_known = "diagnosis=none" not in messages[0].get("content", "")   # system "known state" line
    if not diagnosis_known and not find_tool_results(messages, "get_quota"):
        return tool_call_message("Baik, saya cek kondisi kuota dan jaringan Anda dulu.", "get_quota", {})
    return {"role": "assistant", "content": "Baik. Ada lagi yang bisa saya bantu terkait kuota atau jaringan?"}


def respond(messages: list, tools: list) -> dict:
    """
    Scripted replacement for llm_client.chat().

    Why: §2.11 mock provider — deterministic, offline, valid tool calls for the 5 personas.
    Args:
        messages (list): conversation messages.
        tools (list): tool schemas (unused; same signature as a real call).
    Returns:
        dict: {"message", "tool_calls", "usage"} in the same shape as llm_client.chat().
    Raises / Errors:
        None
    Side effects:
        None
    """
    last = messages[-1]
    if last["role"] == "tool":
        message = after_tool(last["name"], json.loads(last["content"]))
    else:
        message = after_user(messages, last.get("content") or "")
    tool_calls = [{"id": call["id"], "name": call["function"]["name"], "arguments": json.loads(call["function"]["arguments"])}
                  for call in message.get("tool_calls") or []]
    usage = {"prompt_tokens": estimate_tokens(json.dumps(messages) + json.dumps(tools)),
             "completion_tokens": estimate_tokens(json.dumps(message)), "estimated": True}
    return {"message": message, "tool_calls": tool_calls, "usage": usage}
