## nix-csi\.enable



Whether to enable nix-csi\.



*Type:*
boolean



*Default:*
` false `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.authorizedKeys

SSH public keys that can connect to cache and builders



*Type:*
list of string



*Default:*
` [ ] `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.builders\.enable



Whether to enable builder pods\.



*Type:*
boolean



*Default:*
` false `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*
` 1 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.resources



Resource requests/limits for builder pods



*Type:*
attribute set



*Default:*

```
{
  limits = {
    ephemeral-storage = "5Gi";
  };
  requests = {
    cpu = "1";
    ephemeral-storage = "5Gi";
    memory = "2Gi";
  };
}
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.cache\.enable



Whether to enable cache\.



*Type:*
boolean



*Default:*
` false `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.cache\.loadBalancerPort



This option has no description\.



*Type:*
signed integer



*Default:*
` 2222 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.cache\.storageClassName



This option has no description\.



*Type:*
null or string



*Default:*
` null `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.ctest\.enable



Whether to enable ctest\.



*Type:*
boolean



*Default:*
` false `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/ctest\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/ctest.nix)



## nix-csi\.ctest\.replicas



This option has no description\.



*Type:*
signed integer



*Default:*
` 10 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/ctest\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/ctest.nix)



## nix-csi\.hostMountPath



Where on the host to put cknix store



*Type:*
absolute path



*Default:*
` "/var/lib/nix-csi" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.internalServiceName



Internal service name used for reaching builder nodes from cache node



*Type:*
string



*Default:*
` "nix-builders" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.loggingConfig



Python logging configuration dict for nix-csi service\.
See https://docs\.python\.org/3/library/logging\.config\.html\#logging-config-dictschema



*Type:*
JSON value



*Default:*

```
{
  formatters = {
    standard = {
      format = "%(asctime)s %(levelname)s [%(name)s] %(message)s";
    };
  };
  handlers = {
    console = {
      class = "logging.StreamHandler";
      formatter = "standard";
      stream = "ext://sys.stdout";
    };
  };
  loggers = {
    nix-csi = {
      handlers = [
        "console"
      ];
      level = "INFO";
      propagate = false;
    };
  };
  root = {
    handlers = [
      "console"
    ];
    level = "WARN";
  };
  version = 1;
}
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.namespace



Which namespace to deploy cknix resources too



*Type:*
string



*Default:*
` "nix-csi" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.nixBuilderConfig



This option has no description\.



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixBuilderConfig\.checkAllErrors



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixBuilderConfig\.checkConfig



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixBuilderConfig\.extraOptions



This option has no description\.



*Type:*
strings concatenated with “\\n”



*Default:*
` "" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixBuilderConfig\.settings



This option has no description\.



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixCacheConfig



This option has no description\.



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixCacheConfig\.checkAllErrors



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixCacheConfig\.checkConfig



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixCacheConfig\.extraOptions



This option has no description\.



*Type:*
strings concatenated with “\\n”



*Default:*
` "" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixCacheConfig\.settings



This option has no description\.



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixNodeConfig



This option has no description\.



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixNodeConfig\.checkAllErrors



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixNodeConfig\.checkConfig



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixNodeConfig\.extraOptions



This option has no description\.



*Type:*
strings concatenated with “\\n”



*Default:*
` "" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nixNodeConfig\.settings



This option has no description\.



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/config\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/config.nix)



## nix-csi\.nodeBuildTimeout



Timeout in seconds for Nix build operations on node pods\.
Builds exceeding this timeout will be terminated\.



*Type:*
positive integer, meaning >0



*Default:*
` 300 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.privKey



Private SSH key used for in-cluster SSH communication



*Type:*
string



*Default:*

```
''
  -----BEGIN OPENSSH PRIVATE KEY-----
  b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
  QyNTUxOQAAACCUV5k813onHJaerrZpjgy/2pzX3iDGTGo6FNJ4Wlm1JgAAAKBjqJ2wY6id
  sAAAAAtzc2gtZWQyNTUxOQAAACCUV5k813onHJaerrZpjgy/2pzX3iDGTGo6FNJ4Wlm1Jg
  AAAEBnkqzHbwaxtZnHdnJ+OCLKtYWgyHunZ3Ym/GkqYXKKT5RXmTzXeicclp6utmmODL/a
  nNfeIMZMajoU0nhaWbUmAAAAGW5peC1jc2ktZmFsbGJhY2staW5zZWN1cmUBAgME
  -----END OPENSSH PRIVATE KEY-----
''
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.pubKey



Public SSH key used for in-cluster SSH communication



*Type:*
string



*Default:*

```
''
  ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJRXmTzXeicclp6utmmODL/anNfeIMZMajoU0nhaWbUm nix-csi-fallback-insecure
''
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.rsyncConcurrency



Maximum number of concurrent rsync operations when copying store paths\.
Higher values can improve performance but increase I/O load\.



*Type:*
positive integer, meaning >0



*Default:*
` 1 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.undeploy



This option has no description\.



*Type:*
boolean



*Default:*
` false `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.version



This option has no description\.



*Type:*
string



*Default:*
` "0.3.2" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)


