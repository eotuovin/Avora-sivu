# Avora – verkkosivu

Avoran (Muutto365:n järjestelmäpaketti pienyrittäjille) verkkosivu. Tällä hetkellä
testiympäristö: näkyy vain VPN:n kautta osoitteessa http://10.8.0.4:6780 eikä
hakukoneille.

## Rakenne

| Polku | Mitä |
|---|---|
| `design/*.dc.html` | Sivun lähde, sama kuin Claude Designin kankaalla |
| `tools/build_site.py` | Rakentaa `site/`-kansion design-tiedostoista |
| `tools/runtime.js` | Sivun pieni ajonaikainen moottori + kävijäseuranta |
| `site/` | Valmis sivu (generoitu, mutta versioitu jotta palvelin ei rakenna mitään) |
| `server.py` | Palvelin: sivut, lomakkeen vastaanotto, seurannan välitys (vain Pythonin vakiokirjasto) |

Muutos sivuun: päivitä `design/`, aja `python3 tools/build_site.py`, testaa, commitoi `site/` mukana.

## Lomake

`POST /api/esittelypyynto` tallentaa pyynnön ensin SQLiteen (`/data`, Docker-volume),
sitten toimittaa sen Luuriin yrityksenä + yhteyshenkilönä (muistiinpanoissa soittoaika,
paketti, viesti). Jos Luuri ei vastaa tai avain ei vielä toimi, pyyntö odottaa ja
yritetään uudelleen minuutin välein. Tilanne: `GET /api/health`.

Luurin puolella avain otetaan käyttöön `INBOUND_API_KEY`-asetuksella (Luurin PR).

## Kävijäseuranta

Selaimen `avora_*`-tapahtumat → `POST /api/ux` → olemassa oleva ux-events-seuranta
(`ANALYTICS_UX_EVENTS_URL`), merkittynä `site_env=testing` ja `interaction_surface=avora_web`.
Tapahtumat: `avora_page_view`, `avora_cta_click`, `avora_tel_click`, `avora_industry_select`,
`avora_package_select`, `avora_more_details_open`, `avora_faq_open`, `avora_form_submit`
(paketti, soittoaika, yhteystapa, annettiinko lisätiedot, esimerkkiala), `avora_form_error`.
Yhteystietoja ei koskaan lähetetä seurantaan.

## Testit

    python3 -m unittest discover -s tests
