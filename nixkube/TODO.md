# TODO

## Support Nix signing
  * Implement signing paths as soon as the touch cache

## Building
* Wrap distributed building in a nicer "package"
* Better substitution configuration
* Implement speed factor

## Controller
* Rename cache to controller, integrate Kopf for additional future features. (No?)

## NRI
* lower registration timeout to 5s to match default containerd config
* investigate forcing environment variables

## Testing
* NixOS tests!
* Test RO/RW functionality

## Configuration
* Investigate integrating OpenSSH config module from NixOS

## Documentation
* Document all project loggers (grpclib_nri.*, nixkube.*) in doc/logging.md so operators know what to filter/enable per subsystem
