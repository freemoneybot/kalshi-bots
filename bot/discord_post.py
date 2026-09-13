"""Discord webhook posting + the text of every post."""
import json, os, sys, time, urllib.request
from kalshi_core import ASSETS, DISCLAIMER
from engine import tally, tally_line

GREEN, RED, GREY, AMBER = 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF39C12


class WebhookMissing(Exception):
    pass


def webhook_for(asset, cfg):
    per = (cfg.get("per_asset_webhook_urls") or {}).get(asset, "").strip()
    shared = (cfg.get("shared_webhook_url") or "").strip()
    url = per or shared
    if not url:
        raise WebhookMissing(
            "\n"
            "==========================================================\n"
            " NO DISCORD WEBHOOK CONFIGURED - the bot will not start.\n"
            "==========================================================\n"
            f" Asset: {asset}\n"
            " Open config.json and put your webhook URL in either:\n"
            "   \"shared_webhook_url\": \"https://discord.com/api/webhooks/...\"\n"
            f"   or \"per_asset_webhook_urls\": {{ \"{asset}\": \"https://...\" }}\n"
            "\n In Discord: pick the channel -> Edit Channel -> Integrations\n"
            " -> Webhooks -> New Webhook -> Copy Webhook URL.\n"
            "==========================================================\n")
    if not url.startswith("https://discord.com/api/webhooks/") and \
       not url.startswith("https://discordapp.com/api/webhooks/"):
        raise WebhookMissing(
            f"\n The webhook URL configured for {asset} does not look like a Discord\n"
            f" webhook. It should start with https://discord.com/api/webhooks/\n"
            f" Got: {url[:60]}\n")
    return url


def send(asset, cfg, embed):
    if cfg["settings"].get("dry_run"):
        print("\n" + "-" * 64)
        print(f"[DRY RUN -> would POST to Discord webhook for {asset}]")
        print(render_plain(embed))
        print("-" * 64)
        return True
    url = webhook_for(asset, cfg)
    body = json.dumps(dict(username=f"Kalshi {ASSETS[asset]['label']} 15m",
                           embeds=[embed])).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300
        except Exception as e:
            if attempt == 2:
                print(f"!! Discord post FAILED for {asset}: {e}", file=sys.stderr)
                return False
            time.sleep(2 + attempt * 3)


def render_plain(embed):
    out = [embed.get("title", "")]
    if embed.get("description"):
        out.append(embed["description"])
    for f in embed.get("fields", []):
        out.append(f"\n{f['name']}\n{f['value']}")
    if embed.get("footer"):
        out.append("\n" + embed["footer"]["text"])
    return "\n".join(out)


def money(v, dec):
    return f"${v:,.{dec}f}"


# ---------------------------------------------------------------- posts
def open_embed(c):
    d = c["dec"]
    return dict(
        title=f"\U0001F514 {c['label']} \u2014 round open \u00b7 {c['ticker']}",
        color=0x3498DB,
        description=(f"Closes in {c['mins_left']:.0f} min. ONE call this round, at the "
                     f"1-minute mark \u2014 and only if the read is strong; otherwise NO CALL."),
        fields=[
            dict(name="Target line", value=money(c["target"], d), inline=True),
            dict(name="Spot now", value=f"{money(c['spot'], d)}\n`{c['spot_source']}`",
                 inline=True),
            dict(name="Gap", value=f"{'+' if c['gap']>=0 else '-'}{money(abs(c['gap']),d)} "
                                   f"({c['gap_pct']:+.3f}%)", inline=True),
            dict(name="YES bid / ask",
                 value=f"{c['yes_bid']*100:.0f}\u00a2 / {c['yes_ask']*100:.0f}\u00a2", inline=True),
            dict(name="Open interest", value=f"{c['open_interest']:,.0f}", inline=True),
            dict(name="Recent noise",
                 value=(f"~{money(c['sigma'], d)}/min" if c["sigma"] else "warming up"),
                 inline=True),
        ],
        footer=dict(text=DISCLAIMER))


def call_embed(c, rec_tally, mark=12, final=True):
    d = c["dec"]
    if c["call"] == "UP":
        color, head = (AMBER if c["weak"] else GREEN), ("\u2191 WEAK LEAN UP" if c["weak"] else "\u2191 UP")
    elif c["call"] == "DOWN":
        color, head = (AMBER if c["weak"] else RED), ("\u2193 WEAK LEAN DOWN" if c["weak"] else "\u2193 DOWN")
    else:
        color, head = GREY, "\u2014 NO CALL"
    if c["conviction"] == "high":
        head += "  \u2022 HIGH CONVICTION (gap + price action agree)"
    head += f"  \u2022 {c['price_label']}"
    if c.get("reversal_flag"):
        head += "  \u2022 \u26A0 PROJECTED FLIP INCOMING"

    sig_block = "\n".join(f"\u2022 **{s['name']}**: {s['text']}" for s in c["signals"])
    sig_block += (f"\n\u2022 **Verdict**: signals lean {c['signal_lean'].upper()}, "
                  f"gap leans {c['gap_dir'].upper()}"
                  f" \u2014 {'AGREE' if c['signal_lean']==c['gap_dir'] else ('CONFLICT' if c['signal_lean'] in ('up','down') else 'MIXED')}")

    fields = [
        dict(name="Spot vs line",
             value=(f"Spot **{money(c['spot'], d)}**\nLine **{money(c['target'], d)}**\n"
                    f"Gap **{'+' if c['gap']>=0 else '-'}{money(abs(c['gap']),d)}** "
                    f"({c['gap_pct']:+.3f}% of spot)"), inline=True),
        dict(name="Market says",
             value=(f"YES **{c['yes_bid']*100:.0f}\u00a2 / {c['yes_ask']*100:.0f}\u00a2**\n"
                    f"NO **{c['no_bid']*100:.0f}\u00a2 / {c['no_ask']*100:.0f}\u00a2**\n"
                    f"OI {c['open_interest']:,.0f}"), inline=True),
        dict(name="Noise band",
             value=((f"~{money(c['sigma'], d)}/min\nband {money(c['band'], d)} over "
                     f"{c['mins_left']:.1f} min") if c["sigma"] else "not enough history"),
             inline=True),
        dict(name=("THE CALL" if final else f"EARLY READ \u00b7 minute {mark}"),
             value=f"**{head}**", inline=False),
        dict(name="Price of this call", value=(
            f"{c['price_label']}\n{c['price_yes']}  |  {c['price_no']}"), inline=False),
        dict(name="Why", value=c["reason"][:1020], inline=False),
        dict(name="RSI / momentum / gaps / history", value=(
            f"RSI(14, 1m) **{c['rsi']:.0f}** ({c['rsi_state']})\n" if c.get("rsi") is not None
            else "RSI: not enough bars yet\n") + (
            (f"Momentum {c['momentum_multi']['m3']:+,.{d}f}/3m, "
             f"{c['momentum_multi']['m5']:+,.{d}f}/5m, "
             f"{c['momentum_multi']['m15']:+,.{d}f}/15m\n")
            if c.get("momentum_multi") and c["momentum_multi"].get("m15") is not None else "") + (
            f"Distance from the line: {money(abs(c['gap']), d)} = "
            f"{c['dist_vol']:.1f}x per-minute vol\n" if c.get("dist_vol") else "") + (
            f"Nearest unfilled gap below: {money(c['fvg_distance']['below']['distance'], d)} away\n"
            if (c.get("fvg_distance") or {}).get("below") else "") + (
            f"Nearest unfilled gap above: {money(c['fvg_distance']['above']['distance'], d)} away\n"
            if (c.get("fvg_distance") or {}).get("above") else "") + (
            f"Market skew: YES mid {c['skew']['yes_mid']*100:.0f}\u00a2\n"
            if (c.get("skew") or {}).get("yes_mid") else "") + (
            f"This asset's {c['history']['direction'].upper()} setups: "
            f"{c['history']['w']}W-{c['history']['l']}L"
            + (" (small sample)" if c["history"]["small"] else "")
            if c.get("history") and c["history"]["n"] else "No settled history for this setup yet"),
            inline=False),
        dict(name="SIGNALS", value=sig_block[:1020], inline=False),
    ]
    if c["entry_price"]:
        fields.append(dict(
            name="Cost of being right",
            value=(f"Entry {c['entry_price']*100:.0f}\u00a2 + {c['fee']*100:.0f}\u00a2 fee "
                   f"\u2192 you need to be right **{c['breakeven']*100:.0f}%** of the time "
                   f"just to break even."), inline=False))
    if c.get("reversal_flag"):
        fields.append(dict(name="\u26A0 PROJECTED FLIP INCOMING",
                           value=c["reversal_text"][:1020], inline=False))
    fields.append(dict(name="Record", value=rec_tally, inline=False))

    if c.get("scalp"):
        s = c["scalp"]
        fields.append(dict(name="Scalp (separate bucket, never in the record)", value=(
            f"{s['side']} at {s['price']*100:.0f}\u00a2 \u2192 target "
            f"{s['target']*100:.0f}\u00a2 ({s['move_cents']}\u00a2), pays {s['payoff']:.1f}x "
            f"held to settle \u2014 {s['why']}."), inline=False))
    return dict(title=(f"\u23F1 {c['label']} \u00b7 minute {mark} \u00b7 {c['ticker']}"),
                color=color, fields=fields,
                footer=dict(text=DISCLAIMER + f"  |  data: {c['bars_note']}"))


def settle_embed(c, result, correct, rec_tally, t):
    if c["call"] == "NO CALL":
        color, head = GREY, "no call was made"
    else:
        color = GREEN if correct else RED
        head = "CALLED IT \u2705" if correct else "MISSED \u274C"
    hi, pl = t["high_conv"], t["plain"]
    def fmt(x):
        return f"{x['wr']*100:.0f}% ({x['w']}W-{x['l']}L)" if x["wr"] is not None else "no data yet"
    return dict(
        title=f"\U0001F3C1 {c['label']} \u00b7 settled \u00b7 {c['ticker']}",
        color=color,
        description=(f"Market settled **{result.upper()}** \u2014 the call was "
                     f"**{c['call']}** \u2014 {head}"),
        fields=[
            dict(name="Record", value=rec_tally, inline=False),
            dict(name="High conviction vs plain",
                 value=(f"High conviction (gap + signals agreed): {fmt(hi)}\n"
                        f"Plain / weak: {fmt(pl)}\n"
                        f"_Split exists to test whether the price-action layer actually "
                        f"adds anything. It needs hundreds of rounds to say._"), inline=False),
        ],
        footer=dict(text=DISCLAIMER))
