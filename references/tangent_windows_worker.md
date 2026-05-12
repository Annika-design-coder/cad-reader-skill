# Tianzheng/T20 Windows CAD Worker

Use this reference only for DWG files that contain Tianzheng/T20 proxy objects such as `TDbText`, `TDbDimension2`, `TDbSymbCoord`, `TDbSymbElevation`, `TDbMText`, `TDbSymbMultiLeader`, or `TDbAxisLabelSet`.

## Why A Separate Worker May Be Required

The Linux BricsCAD service can read native DWG geometry, text, dimensions, layers, screenshots, and proxy-shell metadata. Tianzheng objects are custom ARX objects. Common Tianzheng/T20 plugins target Windows AutoCAD. AutoCAD ARX binaries cannot be loaded directly into BricsCAD; a BricsCAD-compatible BRX/Object Enabler built by the vendor would be required.

Use a Windows CAD worker when the Agent must extract internal Tianzheng object text, dimensions, coordinates, symbols, or geometry instead of only reporting proxy evidence.

## Target Architecture

```text
Local Agent tool
  -> Linux BricsCAD reader, port 8765
      - general DWG/DXF/PDF extraction
      - render full drawing and regions
      - proxy diagnosis
  -> Windows Tianzheng worker, port 8775
      - AutoCAD + matching T20 plugin/Object Enabler
      - converts/exports Tianzheng objects to ordinary CAD objects
      - returns normalized DWG/DXF or structured extraction evidence
```

The local wrapper checks:

```powershell
$env:CADLIST_TANGENT_WORKER_URL="http://<Windows-CAD-Worker>:8775"
$env:CADLIST_TANGENT_WORKER_TOKEN="<token>"
python scripts\cad_reader_tool.py health
```

## No Worker Available

If no Windows worker is available, the tool enters degraded mode:

```text
proxy_fallback.mode = tangent_proxy_degraded
```

In this mode:

- Ordinary CAD entities remain usable.
- `TDb*` internals are marked as blocked/needs conversion.
- `diagnose` generates `天正T20代理对象转换要求.md`.
- Do not treat proxy internals as final quantity or engineering-risk evidence.

## Windows Server Setup

1. Provision a cloud Windows Server with a GUI desktop. Do not use the user's local CAD workstation unless explicitly approved.
2. Install a supported AutoCAD version for the target T20 plugin.
3. Install the matching T20 Tianzheng plugin/Object Enabler.
4. Open a sample Tianzheng DWG manually once and confirm proxy warnings disappear or Tianzheng commands work.
5. Confirm the plugin can convert/export Tianzheng objects to ordinary CAD entities.
6. Run the worker under a dedicated Windows account with an interactive desktop session. CAD automation is usually not reliable from a non-interactive service account.
7. Expose only the worker HTTP port to trusted callers or place it behind a private network/VPN.

## Acceptance Test

Use the same DWG before and after the Windows worker is configured.

Before worker:

```text
proxy_count > 0
proxy_type_hints includes TDb*
tangent_worker.ok = false
```

After worker:

```text
proxy_count drops or the normalized output has ordinary TEXT/DIMENSION/INSERT/LWPOLYLINE objects
text_count and dimension evidence increase or become locatable
rendered full/region images still match the original drawing
```

Keep both evidence JSON files and rendered PNGs for comparison.
