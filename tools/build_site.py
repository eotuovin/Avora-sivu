"""Build site/ from the Claude Design sources in design/.

    python3 tools/build_site.py

Each design/<Name>.dc.html becomes a plain HTML page that renders the same
template with tools/runtime.js. The canvas-only bits are swapped for the live
ones: artboard links become site paths, and the demo form's "pretend it was
sent" step becomes a real POST to /api/esittelypyynto.
"""
import html
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DESIGN = ROOT / "design"
SITE = ROOT / "site"

PAGES = {"Main.dc.html": "index.html", "Tietosuoja.dc.html": "tietosuoja.html"}
LINKS = {"Tietosuoja.dc.html": "/tietosuoja", "Main.dc.html": "/"}

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%2308090A'/%3E"
    "%3Cpath d='M6 18L12 6L18 18' fill='none' stroke='white' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/%3E"
    "%3Ccircle cx='12' cy='14.2' r='1.7' fill='white'/%3E%3C/svg%3E"
)

DESCRIPTION = "Avora: ajanvaraus, asiakastiedot ja viikon luvut samassa järjestelmässä pienyrittäjälle. Muutto365:n tekemä."

# The design marks the form as sent locally; the live page sends it first.
SUBMIT_FROM = "this.setState({ sent: true, err: '' });"
SUBMIT_TO = """if (this._sending) return;
      this._sending = true;
      const track = window.avoraTrack || function () {};
      const payload = { name: s.name, phone: s.phone, email: s.email, company: s.company, message: s.msg,
        call_time: s.when, package: s.fpkg, consent: s.consent, industry_example: s.ind };
      fetch('/api/esittelypyynto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(() => {
          this._sending = false;
          track('avora_form_submit', { package: s.fpkg || 'ei valittu', call_time: s.when || 'ei valittu',
            contact_method: s.phone.trim() ? 'puhelin' : 'sahkoposti',
            details_given: (s.email.trim() || s.company.trim() || s.msg.trim()) ? 'kylla' : 'ei',
            industry_example: s.ind });
          this.setState({ sent: true, err: '' });
        })
        .catch((err) => {
          this._sending = false;
          track('avora_form_error', { status: String(err && err.message || 'network').slice(0, 20) });
          this.setState({ err: 'Lähetys ei onnistunut. Yritä hetken päästä uudelleen tai soita meille.' });
        });"""


def build_page(src: str, *, is_main: bool) -> str:
    title = re.search(r"<title>(.*?)</title>", src, re.S).group(1).strip()
    xdc = re.search(r"<x-dc>(.*)</x-dc>", src, re.S).group(1)
    helmet = re.search(r"<helmet>(.*?)</helmet>", xdc, re.S).group(1)
    body = re.sub(r"<helmet>.*?</helmet>", "", xdc, flags=re.S)
    logic = re.search(r'<script type="text/x-dc"[^>]*>(.*?)</script>', src, re.S).group(1)

    for a, b in LINKS.items():
        body = body.replace(f'href="{a}', f'href="{b}')
    if is_main:
        if logic.count(SUBMIT_FROM) != 1:
            sys.exit("build: the design's submit step changed; update SUBMIT_FROM in tools/build_site.py")
        logic = logic.replace(SUBMIT_FROM, SUBMIT_TO)
    if "</script" in logic.lower() or "</template" in body.lower():
        sys.exit("build: unexpected closing tag inside the design source")

    return f"""<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="description" content="{html.escape(DESCRIPTION)}">
<meta name="theme-color" content="#08090A">
<title>{html.escape(title)}</title>
<link rel="icon" href="{FAVICON}">
{helmet}
</head>
<body>
<div id="root"></div>
<template id="tpl">{body}</template>
<noscript><p style="color:#EDEDED;font-family:system-ui;padding:24px">Sivu tarvitsee JavaScriptin. Voit myös soittaa meille.</p></noscript>
<script src="/runtime.js"></script>
<script>{logic}
AvoraBoot(Component);
</script>
</body>
</html>
"""


def main() -> None:
    SITE.mkdir(exist_ok=True)
    for name, out in PAGES.items():
        src = (DESIGN / name).read_text(encoding="utf-8")
        (SITE / out).write_text(build_page(src, is_main=(name == "Main.dc.html")), encoding="utf-8")
        print("built", out)
    shutil.copyfile(ROOT / "tools" / "runtime.js", SITE / "runtime.js")
    (SITE / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    print("built runtime.js, robots.txt")


if __name__ == "__main__":
    main()
