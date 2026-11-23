# Branding

- `icon.png` — icona di Wildcat Hub (1024×1024, bossolo in stile synthwave, angoli trasparenti).
- `icon-concept.webp` — originale da cui è ricavata.

Dove viene usata (file generati da `icon.png`):

- `frontend/app/icon.png` (256 px) e `frontend/app/apple-icon.png` (180 px): favicon e icona dell'app;
- `frontend/public/brand/icon-96.png` e `icon-192.png`: logo nell'intestazione e nel footer.

Per cambiarla, sostituisci `icon.png` e rigenera:

```bash
magick branding/icon.png -resize 256x256 -strip frontend/app/icon.png
magick branding/icon.png -resize 180x180 -strip frontend/app/apple-icon.png
magick branding/icon.png -resize 96x96   -strip frontend/public/brand/icon-96.png
magick branding/icon.png -resize 192x192 -strip frontend/public/brand/icon-192.png
```

Per usarla anche come icona del remote Flatpak (`.flatpakrepo`), caricala da
*Amministrazione → Repository → Icona*.
