project('pydd-drv', 'cpp', default_options : ['cpp_std=c++23', 'warning_level=2'])

nix_store = dependency('nix-store')
json = dependency('nlohmann_json')

# `SubmitStore` and the free `derivation::parseJsonAndValidate` arrived after
# 2.35, with `builder-rpc-v0`.
has_submit = nix_store.version().version_compare('>=2.36pre')

executable(
  'pydd-drv',
  'main.cc',
  dependencies : [nix_store, json],
  cpp_args : ['-DPYDD_HAS_SUBMIT=' + (has_submit ? '1' : '0')],
  install : true,
)
