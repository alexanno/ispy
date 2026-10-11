# ispy – "Jeg ser"-oppgaver til fargelegging

Genererer finn-og-tell-ark (A4 som standard) med tellelisten nederst, pluss fasit.

```bash
uv run ispy.py fetch                 # henter ikonpakker (én gang)
uv run ispy.py list                  # temaer
uv run ispy.py make paaske           # én oppgave + fasit  → out/
uv run ispy.py make halloween jul --copies 3
uv run ispy.py make all --seed 42
uv run ispy.py make sommer --legend 30 --cell 12 --formats svg,pdf,png
```

Temaer: paaske, halloween, jul, sommer, host, vinter, var, valentine, bursdag, dyr, rom.

## Nytt tema
Legg til en blokk i `themes.toml` (minst ~28 ikoner):

```toml
[themes.sjoeroever]
subtitle = "Sjørøver"
icons = ["tabler:anchor", "lucide:sailboat", "phosphor:skull", "builtin:egg-plain", ...]
```

Ikonnavn slår du opp som filnavn i `icons/<pakke>/`. Egne SVG-er legges i `icons/custom/` og refereres som `custom:navn`.

## Ikonpakker (vektor, permissive lisenser)
- Tabler Icons (MIT), Lucide (ISC), Phosphor Icons (MIT) – hentes fra npm av `fetch`
- `builtin:egg-*` – egg med mønstre, tegnet i scriptet

## Valg
`--seed` (reproduserbar), `--copies`, `--legend` (antall gjenstander), `--cell` (mm; mindre = flere ikoner),
`--paper A4|A3|Letter`, `--title`, `--subtitle`, `--formats svg,pdf,png`, `--no-solution`.
