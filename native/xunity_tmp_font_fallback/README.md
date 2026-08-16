# Unity 6000 TMP Fallback

This BepInEx 6 IL2CPP plugin asynchronously loads the bundled Noto Sans CJK
TMP asset and registers it as a global TextMesh Pro fallback font.

Build against a generated BepInEx IL2CPP directory:

```powershell
dotnet build .\EngAixt.XUnityTmpFontFallback.csproj -c Release `
  -p:BepInExRoot='C:\path\to\game\BepInEx'
```

Copy the resulting DLL to `assets/xunity_plugins/` for release packaging.
