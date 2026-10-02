# IFC reference compatibility

## Setting

The deployment enables this MicroStation configuration variable:

```text
IFC_ALLOW_DEPRECATED_SCHEMA = 1
```

Bentley documents this workaround in
[KB0098741](https://bentleysystems.service-now.com/community?id=kb_article_view&sysparm_article=KB0098741).
It allows MicroStation and related PowerPlatform products to continue opening,
importing, or attaching trusted IFC files that use deprecated schema
definitions.

The spelling uses underscores. A hyphenated spelling shown in one forum reply
is incorrect.

## Version evidence

- MicroStation CONNECT Update 17, 10.17.01.58: confirmed by the original forum
  user.
- OpenBuildings Designer 24.00.03.14: documented by Bentley KB0098741.
- MicroStation 26.00.01.65: this project verified with MicroStation's
  `-debug=5` configuration report that the installed organization configuration
  resolves `IFC_ALLOW_DEPRECATED_SCHEMA` to `1`.

This does not prove behavior for every Bentley product or release. Releases
that do not recognize the variable are not claimed as supported by this
compatibility step.

## Scope and limitations

The variable relaxes a reader restriction. It does not:

- repair corrupt IFC data;
- convert an old IFC schema to IFC4;
- guarantee complete geometry or properties;
- make a legacy schema valid for an IFC+SG submission;
- change the checker's IFC4 schema rule.

Use it only with IFC files from trusted sources. Correcting and republishing
the source IFC is preferable when that is possible.

## Deployment

`Deploy.bat` enables the setting in the same configuration file used by the
checker, written to every detected product:

```text
<product install>\config\appl\IFCSG_Checker.cfg
```

Products whose `msconfig.cfg` does not include `config\appl` use
`<MicroStation Configuration>\Organization\IFCSG_Checker.cfg` instead.

MicroStation must be restarted after installation.

To install while keeping Bentley's default deprecated-schema restriction:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\tools\microstation\install_ifcsg_checker.ps1 `
  -DisableDeprecatedIfcSchemas
```

## Rollback

Run the installer with `-DisableDeprecatedIfcSchemas`, or uninstall the
checker. Both paths remove the active assignment from `IFCSG_Checker.cfg`.

To verify the effective value in MicroStation, use the Configuration Variables
dialog or generate an `msdebug.txt` report with MicroStation's `-debug=5`
startup option.
