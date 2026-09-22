# Practice works and labaratories on microcontrollers in university

Platform: KiKad
KiCad version: V10
Student: Валекжанин Владимир Сергеевич, group: ЭФБО-04-24
My individual variant: 4 (fourth)
Additional tools: pip3, python3.13, uv

## MCP servers

- `kicad` (via `opencode.jsonc`): KiCAD MCP Server (clone at `~/KiCAD-MCP-Server`, built `dist/index.js`) for schematic/PCB automation. Uses KiCad 10's bundled Python (`KICAD_PYTHON` env var in config). Note: opencode only discovers `opencode.json`/`opencode.jsonc` in the project root — `.opencode/` config files are NOT loaded as project config.

## AI skills (kicad-happy)

- `kicad-happy` skills are loaded project-wide via `opencode.jsonc` → `"skills": { "paths": ["~/github/agentic/kicad-happy/skills"] }` (clone lives OUTSIDE this repo at `~/github/agentic/kicad-happy`; upgrade with `git pull` there).
- 11 skills available: `kicad` (schematic/PCB analysis, design review), `spice` (simulation, needs ngspice/LTspice/Xyce), `emc` (EMC pre-compliance), `datasheets` (PDF spec extraction), `bom`, `digikey`, `mouser`, `lcsc`, `element14`, `jlcpcb`, `pcbway` (sourcing/fabrication).
- Analysis scripts are pure Python 3.10+ stdlib, run directly, e.g. `python3 ~/github/agentic/kicad-happy/skills/kicad/scripts/analyze_schematic.py <file>.kicad_sch`.

## Caveats

- All units MUST be in mm (millimeters). Never use mil, inch, or any other unit in schematic/PCB files, coordinates, footprints, or design rules.
- NEVER read pdf or docx files using 'Read' tool - it immediately breaks opencode and makes you stuck. Always parse them using python, or use docx-mcp if available.
- Keep all schematic/PCB coordinates on the 1.27 mm grid.

## Requirements
- You talk to me in english, only. Never speak to me in russian;
- All source code, reports, and other media intended for my professor must be in russian.

## Practice works

### Requirements

Every practice requires:
- write a docx report (отчет)
- implement an introductory example (учебный пример)
- implement a task by an individual variant (индивидуальный вариант)

# Labaratories
