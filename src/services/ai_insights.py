"""AI Smart Insights (Option A) — turns the business's REAL numbers into a few
plain-English observations + suggestions.

SAFETY (money app): the AI only NARRATES figures the accounting/CRM engine
already computed. `gather()` builds an exact facts dict; `narrate()` feeds those
exact numbers to the model with a strict "never invent a figure" instruction; and
if the AI call fails for ANY reason, `fallback()` returns a deterministic
rule-based summary from the same facts — so the feature never blanks and never
states a number the engine didn't produce.

Pro-gated by the caller (tier check). Cached ~6h per user+period so repeated taps
don't re-bill OpenAI. Reuses the categorizer's OpenAI client pattern.
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_CACHE_HOURS = 6
_MODEL = "gpt-4o-mini"
_MAX_TOKENS = 450


def _money(n):
    try:
        return f"NGN {int(n):,}"
    except Exception:
        return "NGN 0"


class AIInsights:
    def __init__(self, database, session_mgr=None):
        self.db = database
        self.session = session_mgr
        self._client = None

    # ── public entry ────────────────────────────────────────────────────
    def smart_insights(self, phone_number, period="month"):
        """Return the insight TEXT for a period. Uses a ~6h cache; falls back to
        deterministic rules if the AI is unavailable. Never raises."""
        try:
            cached = self._cached(phone_number, period)
            if cached:
                return cached
            facts = self.gather(phone_number, period)
            text = self.narrate(facts)
            if not text:
                text = self.fallback(facts)
            self._store_cache(phone_number, period, text)
            return text
        except Exception as e:
            logger.warning(f"smart_insights failed: {e}")
            try:
                return self.fallback(self.gather(phone_number, period))
            except Exception:
                return ("🧠 *Smart Insights*\n\nNot enough data yet — record a few "
                        "more sales and check back.")

    # ── 1) gather REAL numbers (no AI) ──────────────────────────────────
    def gather(self, phone_number, period="month"):
        """Assemble a compact dict of engine-computed facts. Pure data."""
        from services.accounting import Accounting
        from features.reports import _date_range

        start, end, label = _date_range(period)
        acct = Accounting(self.db, self.session)

        pnl = acct.period_pnl(phone_number, start, end, label) or {}
        cash = acct.period_cashflow(phone_number, start, end, label) or {}
        pos = acct.position(phone_number) or {}
        try:
            margins = acct.product_margins(phone_number, start, end, top=5) or []
        except Exception:
            margins = []
        try:
            trend = acct.profit_trend(phone_number, months=6) or []
        except Exception:
            trend = []

        # CRM signals — reuse the (previously unused) smart CRM logic.
        crm_signals = []
        try:
            from services.crm import ContactService
            crm_signals = ContactService(self.db).generate_insights(phone_number) or []
        except Exception as e:
            logger.debug(f"crm insights unavailable: {e}")

        user = self.db.get_user(phone_number) or {}
        biz = user.get("business_name", "the business")
        industry = user.get("industry_class", user.get("business_type", "trading"))

        # Best / worst margin products (only costed ones).
        costed = [m for m in margins if not m.get("has_uncosted") and m.get("revenue")]
        best = costed[0] if costed else None
        worst = None
        if costed:
            worst = sorted(costed, key=lambda m: m.get("margin_pct", 0))[0]

        return {
            "business": biz,
            "industry": industry,
            "period_label": label,
            "revenue": int(pnl.get("revenue", 0)),
            "cogs": int(pnl.get("cogs", 0)),
            "gross_profit": int(pnl.get("gross_profit", 0)),
            "gross_margin_pct": int(pnl.get("gross_margin_pct", 0)),
            "opex": int(pnl.get("opex", 0)),
            "net_profit": int(pnl.get("net_profit", 0)),
            "uncosted_sales": int(pnl.get("uncosted_count", 0)),
            "sales_count": int(pnl.get("sales_count", 0)),
            "cash_in": int(cash.get("cash_in", 0)),
            "cash_out": int(cash.get("cash_out", 0)),
            "net_cash": int(cash.get("net_cash", 0)),
            "inventory_value": int(pos.get("inventory_value", 0)),
            "receivables": int(pos.get("receivables", 0)),
            "payables": int(pos.get("payables", 0)),
            "best_product": ({"name": best["name"], "margin_pct": best.get("margin_pct", 0),
                              "revenue": best.get("revenue", 0)} if best else None),
            "worst_product": ({"name": worst["name"], "margin_pct": worst.get("margin_pct", 0)}
                              if worst else None),
            "trend": [(lbl, int(v)) for lbl, v in trend],
            "crm_signals": crm_signals[:3],
        }

    # ── 2) deterministic fallback (no AI) ───────────────────────────────
    def fallback(self, f):
        """A rule-based insight summary from the SAME facts. Always safe/correct
        — used when the AI call is unavailable."""
        lines = [f"🧠 *Smart Insights — {f.get('period_label','')}*", ""]
        rev = f.get("revenue", 0)
        net = f.get("net_profit", 0)
        if f.get("sales_count", 0) == 0:
            lines.append("• No sales recorded this period yet. Record sales to see "
                         "profit, margins and trends here.")
            return "\n".join(lines)

        lines.append(f"• Revenue {_money(rev)} · net "
                     f"{'profit' if net >= 0 else 'loss'} {_money(abs(net))} "
                     f"(margin {f.get('gross_margin_pct',0)}%).")
        if f.get("best_product"):
            bp = f["best_product"]
            lines.append(f"• Best margin: *{bp['name']}* at {bp['margin_pct']}%.")
        if f.get("worst_product") and f["worst_product"]["margin_pct"] < 10:
            wp = f["worst_product"]
            lines.append(f"• Thin margin on *{wp['name']}* ({wp['margin_pct']}%) — "
                         f"consider your pricing or cost there.")
        if f.get("uncosted_sales", 0) > 0:
            lines.append(f"• {f['uncosted_sales']} sale(s) have no cost recorded — "
                         f"profit may be overstated. Add the cost for accuracy.")
        if f.get("receivables", 0) > 0:
            lines.append(f"• {_money(f['receivables'])} owed to you — chase debtors "
                         f"to free up cash.")
        if f.get("payables", 0) > 0:
            lines.append(f"• You owe {_money(f['payables'])} to suppliers.")
        # Trend nudge.
        tr = f.get("trend", [])
        if len(tr) >= 2 and tr[-2][1] and tr[-1][1] < tr[-2][1]:
            lines.append(f"• Net profit dipped vs {tr[-2][0]} — worth a look.")
        for s in f.get("crm_signals", []):
            lines.append(f"• {s}")
        return "\n".join(lines)

    # ── 3) AI narration (guarded) ───────────────────────────────────────
    def narrate(self, facts):
        """Ask the model to narrate ONLY the given figures. Returns text or None
        (caller then uses fallback). Never raises."""
        try:
            from openai import OpenAI
            from utils.config import get_openai_key
            if self._client is None:
                self._client = OpenAI(api_key=get_openai_key())

            system = (
                "You are Kashia, a friendly, practical business advisor for a "
                "Nigerian SME. You are given a JSON object of EXACT, already-"
                "computed figures for a period. Write 3–5 short, specific bullet "
                "observations, each with one concrete, actionable suggestion. "
                "RULES: Use ONLY the numbers provided. Never invent, estimate, or "
                "recompute any figure. If a value is 0 or missing, don't mention "
                "it. Amounts are in Nigerian Naira — write them as 'NGN 1,234'. "
                "Be concise and encouraging; no preamble, just the bullets. Start "
                "with a one-line headline."
            )
            user = json.dumps(facts, default=str)
            resp = self._client.chat.completions.create(
                model=_MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_tokens=_MAX_TOKENS,
                temperature=0.4,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                return None
            # Prefix a header if the model didn't add one.
            if "insight" not in text.lower()[:40] and not text.startswith("🧠"):
                text = f"🧠 *Smart Insights — {facts.get('period_label','')}*\n\n{text}"
            return text
        except Exception as e:
            logger.warning(f"AI narrate failed (using fallback): {e}")
            return None

    # ── cache (per user + period, ~6h) ──────────────────────────────────
    def _cached(self, phone_number, period):
        try:
            user = self.db.get_user(phone_number) or {}
            c = user.get("ai_insight_cache") or {}
            if c.get("period") != period:
                return None
            ts = c.get("at")
            if not ts:
                return None
            age_h = (datetime.now() - datetime.fromisoformat(str(ts))).total_seconds() / 3600.0
            if age_h <= _CACHE_HOURS and c.get("text"):
                return c["text"]
        except Exception:
            return None
        return None

    def _store_cache(self, phone_number, period, text):
        try:
            self.db.update_user(phone_number, {
                "ai_insight_cache": {
                    "period": period,
                    "at": datetime.now().isoformat(),
                    "text": text,
                }
            })
        except Exception as e:
            logger.debug(f"ai insight cache store failed: {e}")
