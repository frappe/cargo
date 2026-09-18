# CRITICAL SYSTEM INSTRUCTION

BEFORE PROCEEDING WITH ANY ACTION OR CODE GENERATION, ACKNOWLEDGE AND ADHERE TO THE FOLLOWING RULES.

## Code Commenting & Documentation Directives

1. **No Redundant Comments**
   - DO NOT write line-by-line comments that simply restate what the code does.
   - Self-documenting code (clear naming, logical structure) requires NO inline comments.

2. **Docstrings & Headers**
   - Keep docstrings/JSDoc brief and high-level (1-2 sentences maximum).
   - Omit trivial type repeats, parameter lists, and return descriptions when the function signature already makes them obvious.
   - Avoid massive multi-line docstrings for internal or simple helper functions.

3. **Existing Code Refactoring**
   - When modifying existing code, strip out unnecessary comments and excessive docstrings to keep the file lean.

4. **Before making any massive architectural changes**
    - Always consult and inform the user before making any architectural plan.

5. **Beyond scope implementation**
    - In case something is beyond the scope of the given task, but is a actual issue, inform the user of it before making change to it directly.
