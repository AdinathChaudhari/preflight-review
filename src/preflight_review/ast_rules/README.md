# Bundled ast-grep rules

Packaged rules, installed into a repo with `preflight-review init --with-ast-rules`
(copies `rules/*.yml` into `.preflight/ast-rules/`). Rules in `.preflight/ast-rules/` run on
every review when `tools.ast_grep` is on. An extension module can load further packaged rule
folders without `init` (see `preflight_review/extensions/`); a rule already copied into
`.preflight/ast-rules/` under the same file name is not loaded twice.

`ast-grep scan` loads EVERY `*.yml` in a rule directory as a rule. Test cases therefore live
in `tests/`, never in `rules/`, and must never be copied into `.preflight/ast-rules/`.

## Rules

| id | catches |
|---|---|
| `no-yaml-unsafe-load` | `yaml.load(...)` without `Loader=SafeLoader`, `yaml.SafeLoader` or `CSafeLoader` |
| `no-shell-true` | `subprocess.<fn>(..., shell=True)` |

## Running the rule tests

Create an `sgconfig.yml` pointing at both directories (absolute paths or relative to it):

```yaml
ruleDirs: [rules]
testConfigs:
  - testDir: tests
```

Then: `ast-grep test -c sgconfig.yml --skip-snapshot-tests`

## Limitations

- `no-yaml-unsafe-load`: only a `Loader=` keyword is recognised. A positional loader
  (`yaml.load(text, yaml.SafeLoader)`) is flagged, a false positive. An aliased import
  (`from yaml import load`) is not matched.
- `no-shell-true`: only the literal `shell=True` keyword is matched. `shell=flag` and
  `**kwargs` are not. Only calls spelled `subprocess.<fn>(...)` match.
