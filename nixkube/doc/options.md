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
list of (string or absolute path)



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



## nix-csi\.builders\.enableProxy



Whether to enable external access to builder pods\.



*Type:*
boolean



*Default:*
` false `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.daemonsets



This option has no description\.



*Type:*
attribute set of (submodule)



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.daemonsets\.\<name>\.enable



Whether to enable builder pods\.



*Type:*
boolean



*Default:*
` true `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.daemonsets\.\<name>\.arch



GOARCH / kubernetes\.io/arch to deploy to



*Type:*
non-empty string



*Default:*
` "amd64" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.daemonsets\.\<name>\.labels



Pod labels



*Type:*
attribute set of string



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.daemonsets\.\<name>\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*
` 1 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.daemonsets\.\<name>\.resources



Resource requests/limits for builder pods



*Type:*
JSON value



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



## nix-csi\.builders\.deployments



This option has no description\.



*Type:*
attribute set of (submodule)



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.deployments\.\<name>\.enable



Whether to enable builder pods\.



*Type:*
boolean



*Default:*
` true `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.deployments\.\<name>\.arch



GOARCH / kubernetes\.io/arch to deploy to



*Type:*
non-empty string



*Default:*
` "amd64" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.deployments\.\<name>\.labels



Pod labels



*Type:*
attribute set of string



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.deployments\.\<name>\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*
` 1 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.deployments\.\<name>\.resources



Resource requests/limits for builder pods



*Type:*
JSON value



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



## nix-csi\.builders\.nixConfig



nix\.conf for builder pods



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*
` "" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nix-csi\.builders\.privilegedSandboxedBuilds



To set up the sandbox Nix must run with privileges, without the sandbox Nix builds can run unprivileged



*Type:*
boolean



*Default:*
` true `

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



Port to run public SSH on for Nix cache



*Type:*
null or signed integer



*Default:*
` 2222 `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.cache\.nixConfig



nix\.conf for cache pod



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.cache\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*
` "" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.cache\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.cache\.storageClassName



Which SC to use, defaults to null which will use default SC



*Type:*
null or string



*Default:*
` null `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nix-csi\.deploySecrets



This option has no description\.



*Type:*
boolean



*Default:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.hostMountPath



Where on the host to put nix-csi store, / is untested and not recommended



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



## nix-csi\.knownHosts



SSH host keys to accept when connecting



*Type:*
attribute set of (string or absolute path)



*Default:*
` { } `

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
      format = "%(levelname)s [%(name)s] %(message)s";
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
    httpx = {
      handlers = [
        "console"
      ];
      level = "WARNING";
      propagate = false;
    };
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



## nix-csi\.metadata



Labels added to nix-csi resources



*Type:*
JSON value



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.namespace



Which namespace to deploy nix-csi to



*Type:*
string



*Default:*
` "nix-csi" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nix-csi\.node\.enable



Whether to enable cache\.



*Type:*
boolean



*Default:*
` true `



*Example:*
` true `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nix-csi\.node\.nixConfig



nix\.conf for CSI/mounter/DaemonSet pods



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nix-csi\.node\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*
` "" `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nix-csi\.node\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*
` { } `

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nix-csi\.nodeBuildTimeout



Timeout in seconds for Nix build operations on node pods\.
Builds exceeding this timeout will be terminated\.



*Type:*
positive integer, meaning >0



*Default:*
` 300 `

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


