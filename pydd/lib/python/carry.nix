/**
  `value`, with each string that carries context as `{ text, __context }`,
  for JSON. `__context` is what `builtins.getContext` gives, and a planner's
  decoders put it back (`pydd.modes.render._render.carried`,
  `pydd.modes.evaluate._document.document_value`, `lib/modes/nix-eval.nix`).
*/
value:
let
  encode =
    value:
    if builtins.isString value && builtins.hasContext value then
      {
        text = builtins.unsafeDiscardStringContext value;
        __context = builtins.getContext value;
      }
    else if builtins.isList value then
      map encode value
    else if builtins.isAttrs value then
      builtins.mapAttrs (_: encode) value
    else
      value;
in
encode value
