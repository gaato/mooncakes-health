# mooncakes health

Can you use the newest version of each [mooncakes.io](https://mooncakes.io) module with the current `moon`?

**Page: https://gaato.github.io/mooncakes-health**

Every day, each module's newest non-yanked version is added to an empty module, every package a user can import from it is imported, and `moon check` runs on the module's preferred target (wasm-gc if it names none), with the `latest` MoonBit toolchain.

| Mark | Meaning |
|---|---|
| ○ | Usable as is. |
| ! | Usable after adding a newer version of a dependency to your `moon.mod`. The module itself is fine, but it requires a dependency version whose `moon.mod.json` or `moon.pkg.json` moon now rejects. The page shows the lines to add. |
| ✕ | Not usable without a new release: its own manifest is JSON, a dependency's manifest is JSON with no newer version to use instead, a required version is missing from the registry, or `moon check` fails. |
| ? | Could not be checked this time, for example because a download failed. |

A module is checked again when a new version is published or the toolchain changes. Status of every module: [`status.json`](https://gaato.github.io/mooncakes-health/status.json).

## Limits

- Only the consumer's view is checked: a module's own tests are not compiled.
- The suggested versions are the oldest newer ones in the new format, preferring the same release line, and the page only lists a workaround after `moon check` passed with it.
- Modules are checked one after another in a few shared jobs. Build scripts of a module run there, so a module could in principle affect the result shown for another module checked after it in the same job.

## License

[Blue Oak Model License 1.0.0](LICENSE.md)
