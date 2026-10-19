# Assets

## `nixidae.svg`

The Nixidae mark: the NixOS lambda-snowflake with Python's two-serpent glyph at
its centre.

- **Format:** SVG, `1024×1024` viewBox, no external references, no embedded
  raster data. It scales to a favicon and to a conference banner from the same
  file.
- **Colours:** the gradients are the upstream ones. The outer arms use the
  official NixOS gradient vector and its 0 % / 25 % / 100 % stop construction;
  the blue and yellow ramps use Python's endpoint colours (`#306998`→`#5A9FD4`
  and `#FFD43B`→`#FFE873`) fitted to the same three-stop shape.
- **Background:** none. It sits on light and dark backgrounds equally well,
  which is why the organisation avatar has no plate behind it.

### Using it

In `profile/README.md` and anywhere else GitHub renders it, reference it by
absolute URL — relative paths do not resolve on the organisation profile page:

```
https://raw.githubusercontent.com/nixidae/.github/main/assets/nixidae.svg
```

### Derivation and marks

The mark is derived from two upstream logos:

- The **NixOS logo**, © NixOS contributors, licensed
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) —
  <https://github.com/NixOS/nixos-artwork>
- The **Python logo**, © Python Software Foundation.

#### Required disclaimer

The Python Software Foundation granted permission for this specific use — the
Python logo embedded in the Nix logo — in August 2026, on condition that the
following is stated wherever the mark appears. Reproduce it verbatim if you use
the mark anywhere new:

> "Python" and the Python logos are trademarks of the Python Software
> Foundation, used with permission. Nixidae is not affiliated with the Python
> Software Foundation.

All three parts are required by the grant: that the PSF owns the marks, that we
are unaffiliated, and that the use is *with permission*. Dropping any one of
them puts the use outside what was approved — in particular, do not soften "with
permission" into a general appeal to the PSF's
[trademark usage policy](https://www.python.org/psf/trademarks/), because this
use rests on the specific grant rather than on the policy's blanket allowances.

The approval email is on file with the organisation owner.

The same non-affiliation applies to the NixOS side: Nixidae is not affiliated
with, endorsed by, or sponsored by the NixOS Foundation.

The disclaimer currently appears in the footer of
[`profile/README.md`](../profile/README.md), which is the org landing page and
the most visible place the mark is used, and in the footer of the repository
[`README.md`](../README.md). Anywhere else the mark goes — a docs site, a
conference slide, a favicon on a project page — needs it too.

### Licence of this file

Because the NixOS geometry is CC BY-SA, treat `nixidae.svg` as CC BY-SA 4.0
rather than under the repository's MIT licence: reuse it, but keep the
attribution and share modifications alike. Note that the CC licence covers the
*copyright* in the artwork only — it grants nothing in respect of the PSF's
trademarks, which is what the permission above is for.
