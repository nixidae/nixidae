## nixkube\.enable



Whether to enable nixkube\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.authorizedKeys

SSH public keys that can connect to cache and builders



*Type:*
list of (string or absolute path)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.builders\.enable



Whether to enable builder pods\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets



This option has no description\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.enable



Whether to enable builder pods\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.arch



GOARCH / kubernetes\.io/arch to deploy to



*Type:*
non-empty string



*Default:*

```nix
"amd64"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.labels



Pod labels



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*

```nix
1
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.resources



Resource requests/limits for builder pods



*Type:*
JSON value



*Default:*

```nix
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



## nixkube\.builders\.deployments



This option has no description\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.enable



Whether to enable builder pods\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.arch



GOARCH / kubernetes\.io/arch to deploy to



*Type:*
non-empty string



*Default:*

```nix
"amd64"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.labels



Pod labels



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*

```nix
1
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.resources



Resource requests/limits for builder pods



*Type:*
JSON value



*Default:*

```nix
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



## nixkube\.builders\.loadBalancerPort



Port to run public SSH on for builder jumpbox



*Type:*
null or (positive integer, meaning >0)



*Default:*

```nix
2223
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.nixConfig



nix\.conf for builder pods



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.privilegedSandboxedBuilds



To set up the sandbox Nix must run with privileges, without the sandbox Nix builds can run unprivileged



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.cache\.enable



Whether to enable cache\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.loadBalancerPort



Port to run public SSH on for Nix cache



*Type:*
null or signed integer



*Default:*

```nix
2222
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.nixConfig



nix\.conf for cache pod



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.storageClassName



Which SC to use, defaults to null which will use default SC



*Type:*
null or string



*Default:*

```nix
null
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.deploySecrets



This option has no description\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.hostMountPath



Where on the host to put nixkube store, / is untested and not recommended



*Type:*
absolute path



*Default:*

```nix
"/var/lib/nix-csi"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.internalServiceName



Internal service name used for reaching builder nodes from cache node



*Type:*
string



*Default:*

```nix
"nix-builders"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.knownHosts



SSH host keys to accept when connecting



*Type:*
attribute set of (string or absolute path)



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.loggingConfig



Python logging configuration dict for nixkube service\.
Merged with built-in defaults, so you only need to override specific parts\.
See https://docs\.python\.org/3/library/logging\.config\.html\#logging-config-dictschema



*Type:*
attribute set of (JSON value)



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.metadata



Metadata (labels, annotations) applied to nixkube resources



*Type:*
JSON value



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.namespace



Which namespace to deploy nixkube to



*Type:*
string



*Default:*

```nix
"nixkube"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.node\.enable



Whether to enable cache\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.csi\.compat\.enable



Whether to enable nix\.csi\.store CSI driver (for backwards compatibility)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.nixConfig



nix\.conf for CSI/mounter/DaemonSet pods



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.nodeBuildTimeout



Timeout in seconds for Nix build operations on node pods\.
Builds exceeding this timeout will be terminated\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
300
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.rsyncConcurrency



Maximum number of concurrent rsync operations when copying store paths\.
Higher values can improve performance but increase I/O load\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
1
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.undeploy



This option has no description\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.version



This option has no description\.



*Type:*
string



*Default:*

```nix
"0.4.3"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)


