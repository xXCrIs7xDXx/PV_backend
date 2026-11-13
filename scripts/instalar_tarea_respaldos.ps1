<#
Registra (o quita) la tarea "PV - Respaldos Drive" en el Programador de tareas.

La tarea arranca el agente al iniciar sesion y lo mantiene vivo:
  venv\Scripts\pythonw.exe -m app.backups.agente servicio
El propio agente decide cuando ejecutar (BACKUPS_HORA_DIARIA, recupera la del
dia si el PC estaba apagado) y atiende "Ejecutar ahora" desde el panel.

Corre con TU usuario y solo con la sesion iniciada: Google Drive para
escritorio monta G: dentro de la sesion; como servicio de Windows (sin
sesion) G: no existiria.

Uso (PowerShell, desde la carpeta PV):
  .\scripts\instalar_tarea_respaldos.ps1              # instala / actualiza y arranca
  .\scripts\instalar_tarea_respaldos.ps1 -Desinstalar  # la quita
#>
param([switch]$Desinstalar)

$ErrorActionPreference = "Stop"
$nombre = "PV - Respaldos Drive"
$pv = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $pv "venv\Scripts\pythonw.exe"

if ($Desinstalar) {
    Stop-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
    Write-Host "Tarea '$nombre' eliminada."
    return
}

if (-not (Test-Path $pythonw)) { throw "No existe $pythonw (crea el venv e instala requirements.txt)" }
if (-not (Test-Path (Join-Path $pv ".env"))) { throw "Falta $pv\.env (DATABASE_URL, ADMIN_PASSWORD y BACKUPS_*)" }

$usuario = "$env:USERDOMAIN\$env:USERNAME"
$accion = New-ScheduledTaskAction -Execute $pythonw -Argument "-m app.backups.agente servicio" -WorkingDirectory $pv
$disparador = New-ScheduledTaskTrigger -AtLogOn -User $usuario
$disparador.Delay = "PT2M"  # deja que Google Drive monte G: primero
$opciones = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $disparador -Settings $opciones `
    -Principal $principal -Description "Descarga y audita los respaldos de Backupumsa2627 (Google Drive) a D:. Ver README del backend PV." `
    -Force | Out-Null
Start-ScheduledTask -TaskName $nombre
Write-Host "Tarea '$nombre' registrada y en marcha."
Write-Host "Mensajes del agente: D:\Backups\Registros\agente.log (o la ruta de BACKUPS_DESTINO_RAIZ)."
