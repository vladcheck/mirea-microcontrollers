# Practice works and labaratories on microcontrollers in university

Platform: KiKad
My individual variant: 4 (fourth)
Additional tools: pip3, python3.13, uv

## Caveats

- NEVER read pdf or docx files using 'Read' tool - it immediately breaks opencode and makes you stuck. Always parse them using python, or use docx-mcp if available.
- After creating or editing any `.kicad_sch` file, validate it with `python3 tools/validate_kicad_sch.py <file>`. Checks: lexical (no `;` comments — KiCad s-expressions do NOT support them, balanced parens, uuids) and layout (pin-wire connectivity, unintended junctions, symbol body overlaps, wire-through-body, text overlap, sheet utilization, 1.27 mm grid alignment). Errors fail the run; warnings only fail with `--strict`. Keep all coordinates on the 1.27 mm grid.

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
