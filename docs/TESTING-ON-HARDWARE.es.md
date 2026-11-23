# Probar el driver de desarrollo 3.0.x en una tarjeta

Esta lista sirve para paquetes nuevos, incluida la 3.0.13, que no ha corrido en una tarjeta. En una RTX 5050 las
versiones hasta la 3.0.12 se instalaron y arrancaron, las pruebas de tarjeta corrieron en la 3.0.1 y en la 3.0.7 (133
filas, 130 pasan) y GPU Caps tiene evidencia física de alcance limitado.
Consulta [STATUS.md](STATUS.md) y [BUILDING.es.md](BUILDING.es.md). Cada "bien" de abajo es un criterio de
aceptación, no un resultado confirmado del árbol actual. Guarda cada resultado con la revisión exacta del paquete.

Después de arrancar, valida GPU Caps únicamente mediante lecturas según [GPU-CAPS.md](GPU-CAPS.md).
El control manual de ventiladores pertenece a NVMetrics; consulta [NVMETRICS.md](NVMETRICS.md).

In English: [TESTING-ON-HARDWARE.md](TESTING-ON-HARDWARE.md). La versión en inglés es la de referencia.

Los pasos van en el orden en que te los encuentras: antes de instalar, el primer arranque, el escritorio, juegos y
video, reposo y activación, y dos GPU si las tienes. Cada uno dice qué es "bien" y qué es "mal". Cuando uno sale mal,
para ahí: anota lo que viste, usa la vuelta atrás de ese cambio (la tabla del final) durante un arranque o un
lanzamiento de la aplicación, y di en el informe si el problema desapareció con ella. Lo que significa cada línea y
cada contador está explicado una sola vez, en el documento que enlaza cada paso (en inglés); esta página solo dice qué
ejecutar y qué esperar. [TROUBLESHOOTING.md](TROUBLESHOOTING.md), "The new paths of release 3.0", dice qué hacer con
cada tipo de fallo.

## Dónde escribe el driver

- **Kernel**: `log show --last boot --predicate 'process == "kernel"'` (cada `log show --last boot` tarda cerca de
  un minuto). Las líneas de NVRM empiezan por `NVRM-xnu:`,
  las del framebuffer por `NVRM-fb:`, las del acelerador por `NVAccel:`. Los contadores son sysctl bajo `debug.`
  (`sysctl debug | grep nvrm_`, `sysctl debug | grep nvrmfb_` y `sysctl debug | grep nvaccel_`).
- **El plugin Metal** (lo que cada aplicación y WindowServer hacen con Metal): un paquete compilado con `./build.sh`
  lleva el plugin de versión final (release), que escribe su propio registro, `nvmtl.log`, solo para un proceso
  lanzado con `NVMTL_PROCESS_ENV=1` en su entorno (no como root). Para cualquier otro proceso, WindowServer incluido,
  solo las líneas que informan de un fallo (contienen palabras como FAIL, refused, fault, missing o cannot) llegan al
  registro del sistema, con el prefijo `NullMoth:`:
  `log show --last boot --predicate 'eventMessage CONTAINS "NullMoth:"'`. Una aplicación lanzada desde Terminal con
  `NVMTL_PROCESS_ENV=1` escribe `/tmp/nvmtl.log` (o `nvmtl.log` en su `$TMPDIR` cuando no puede escribir en `/tmp`).
  Todo paso de abajo que busca una línea "en `nvmtl.log`" necesita un lanzamiento así; para WindowServer, usa el
  registro del sistema. Las líneas propias de NVK (`nvk-reason: E ...` y `nvk-reason: W ...`) siguen una regla propia:
  sus errores y las dos líneas de una reconstrucción de canal (`nvrm channel slot N rebuilt after ...`,
  `nvrm channel slot N not rebuilt: ...`) llegan al registro del sistema; sus demás avisos no, contengan las palabras
  que contengan.
- **NVK** (la capa Vulkan bajo el plugin) imprime sus líneas, que empiezan por `nvrm:`, en la salida de error de la
  aplicación: lanza la aplicación desde Terminal para verlas.

Los ajustes de `/Library/GPUBundles/nvmtl/nvrm610.conf` van en tu propio bloque al final del archivo, entre
`# --- my settings, kept on update ---` y `# --- end my settings ---` (la sección 1, paso 5, dice por qué y qué
conserva una instalación). Escribe cada uno como `NOMBRE=valor` desde la primera columna, sin ningún espacio en la
línea: el lector del plugin perdona un espacio antes del nombre, antes del `=` y después del valor, el lector propio
de NVK no perdona ninguno, y un programa que use Vulkan sin Metal puede entonces no ver el ajuste o leer otro valor.
Gana la última línea de cada nombre, y un
proceso lee el archivo al arrancar (reinicia la aplicación, o cierra sesión y vuelve a entrar para WindowServer). Los
argumentos de arranque van en la línea `boot-args` del `config.plist` de OpenCore (README.md, "Wiring into an existing
OpenCore setup") y necesitan reiniciar. Quita cada ajuste de prueba cuando termines su prueba.

## 1. Antes de instalar

1. **Una vuelta atrás que hayas probado.** Ten a mano el paquete descomprimido de la versión sobre la que instalas
   (2.9): instalarlo de nuevo con su propio `install.sh` devuelve cada archivo a su sitio
   ([BUILDING.md](BUILDING.md), "Installing the result and going back", que también dice qué hace y qué no hace
   `uninstall.sh`). Antes de cambiar nada, el instalador copia lo que está instalado ahora (los kexts, el bundle del
   plugin, la carpeta `/Library/GPUBundles/nvmtl` con su archivo de ajustes, la colección del kernel, el firmware) en
   una carpeta nueva `/Library/NullMoth/backup-<fecha>-<hora>.<aleatorio>`, cuyo nombre imprime en su línea
   `== 4. back up what is there now -> ...`; anótalo. Si vuelves a ejecutar el instalador antes de un arranque que
   haya encendido la pantalla, la vuelta atrás sigue siendo la carpeta de la primera ejecución, y su paso `== 6b.`
   lo dice. También deja una tarea que se ejecuta en cada arranque: cuando el driver nuevo se apagó solo tras dos
   arranques seguidos que nunca encendieron su pantalla (o tras caerse WindowServer una y otra vez en la pantalla
   NVIDIA), o cuando su framebuffer rechazó las clases de Apple de este macOS (sección 2, paso 3), la tarea devuelve
   todo desde esa carpeta y reinicia una vez (tras un rechazo te deja a ti el reinicio cuando otra GPU muestra una
   pantalla y has iniciado sesión: sección 2, paso 1); `/Library/NullMoth/lastgood.log` dice cuál de los dos motivos
   fue (tras caerse WindowServer una y otra vez su línea es la de los dos arranques; las caídas mismas quedan en
   `/Library/NullMoth/bootguard.log`). Tras un rechazo solo lo hace mientras el Mac siga en la compilación de macOS en
   la que se instaló, así que no actualices macOS entre la instalación y estas pruebas. Se borra sola en el primer
   arranque después de aquel en que encontró la pantalla encendida, así que no ayuda con un problema que aparezca más
   tarde. Ten claro cómo arrancarás el Mac si el escritorio no aparece ([TROUBLESHOOTING.md](TROUBLESHOOTING.md),
   "4. Getting back in"; las entradas de 1401 en el selector de arranque solo existen en un Mac preparado con la app
   1401). Ten claro también cómo editarás el `config.plist` de OpenCore cuando macOS no arranque (desde la Terminal de
   Recuperación o desde otro sistema, con la partición EFI montada): un argumento de arranque que pusiste para una
   prueba solo se puede quitar desde allí. Activa Sesión remota (Configuración del Sistema, o Ajustes del
   Sistema según el idioma, General, Compartir) y comprueba que llegas al Mac por `ssh` desde otra computadora: varios
   fallos de abajo dejan el Mac sin escritorio o con el escritorio congelado, y entonces los registros se leen por la
   red.
   Bien: has arrancado el Mac una vez por esa vuelta atrás y has entrado por `ssh`. Mal: alguna de las dos no
   funciona; no sigas.
2. **Un punto de partida limpio.** `nvram boot-args` muestra la línea `boot-args`: debe tener la línea de instalación
   de README.md y nada más del driver salvo los ajustes que elegiste para la máquina, como `nvfbhidpi=1` (`gfxrst=4`
   se queda si WhateverGreen lo necesita). Tu bloque de `nvrm610.conf` (paso 5) no debe tener ningún ajuste de
   prueba. Quita lo que dejó una prueba anterior, reinicia y vuelve a comprobarlo.
3. **Números de 2.9.** En la versión que funciona ahora, guarda lo que informa el driver, para comparar después:

   ```bash
   sw_vers > ~/before-3.0.txt; nvram boot-args >> ~/before-3.0.txt
   sysctl debug | grep -e nvrm -e nvaccel >> ~/before-3.0.txt
   ```

   Apunta cuánto tarda el escritorio en aparecer después del selector, y el uso de CPU de WindowServer con el
   escritorio en reposo y con un video en marcha (Monitor de Actividad).
4. **El paquete.** La versión 3.0 no tiene paquete ya compilado: compila uno a partir de esta rama
   ([BUILDING.es.md](BUILDING.es.md), "Inicio rápido"), con el paquete de la versión 2.9 como base:

   ```bash
   ./build.sh --base-package ~/Downloads/nullmoth-nvidia-2.9.tar.gz package
   ```

   Prepara el paquete en `package/pkgroot` de su directorio de trabajo y un archivo comprimido
   `nullmoth-nvidia-local-<commit>.tar.gz`, no instala nada y termina con las órdenes de este paso y de la sección 2,
   paso 1. En el Mac Intel nombrado arriba el paquete tenía 101 archivos (214 MB), y después de la compilación nada
   había cambiado fuera del directorio de trabajo, así que compilar no toca la vuelta atrás del paso 1. El driver
   entero se ha compilado allí con Xcode 26.5 en macOS 26.7, y en la máquina de GitHub con Xcode 26.3 (SDK 26.2, el
   Xcode más nuevo que funciona en macOS 15); Xcode 26.0 a 26.2 no se han probado. En Apple silicon,
   `./build.sh deps` toma sus herramientas de Homebrew, un camino que solo se ha ejecutado contra sustitutos, y allí no
   se ha hecho ninguna compilación completa. Nada de lo que compila `./build.sh` pide un macOS posterior a 15.0: el
   traductor de shaders está hecho para 10.12 y posteriores, los dos programas de la caché de DLSS para 12.0, todo lo
   demás que declara un mínimo para 15.0, y los kexts no declaran ninguno. Así que un paquete compilado en macOS 26
   está hecho también para macOS 15; las comprobaciones de las clases de Apple de la sección 2, paso 3, deciden en cada
   macOS en su primer arranque. En ese Mac `./build.sh deps` tardó 46,5 minutos en un directorio de trabajo vacío en
   el commit 650db45, descargas incluidas; en el commit 3933ff6 la compilación de los 17 componentes tardó 5 minutos
   41 segundos y el paquete 27 segundos, y el directorio de trabajo ocupaba después unos 8 GB.
   Después, desde `pkgroot`, `sudo env CHECK=1 ./install.sh` comprueba el paquete y el Mac y no cambia nada. Que pase no
   dice nada del firmware, que la instalación de verdad toma de la instalación anterior o descarga de NVIDIA antes de
   cambiar nada. No se ha informado de ningún resultado suyo con un paquete hecho por `./build.sh`. Bien: su última
   línea es `CHECK PASS (nothing changed)`. Mal: una línea `STOP:` dice qué rechazó; arréglalo primero.
5. **Tu archivo de ajustes.** Léelo antes de instalar: `cat /Library/GPUBundles/nvmtl/nvrm610.conf`. El instalador
   reemplaza la carpeta `/Library/GPUBundles/nvmtl` entera por la del paquete, y de tu archivo anterior solo vuelve a
   poner, al final del archivo del paquete, las líneas que están entre `# --- my settings, kept on update ---` y
   `# --- end my settings ---` (cada marca escrita exactamente así, sola en su línea), y de ellas solo las líneas
   `NOMBRE=valor` cuyo nombre empieza por `NVMTL_`, `NVK_` o `NVRM_` y cuyo valor solo tiene letras, dígitos y
   `. _ , = * -`. Todas las demás líneas se reemplazan por el archivo del paquete: un archivo sin las dos marcas (el
   de 2.9 no las tiene) sale de la instalación igual al archivo del paquete. Una línea cuyo nombre no tiene ninguno de
   los tres prefijos no la lee nadie. Tu archivo anterior no se pierde: es `nvmtl/nvrm610.conf` dentro de la carpeta
   de respaldo del paso 1. Para conservar una línea tuya, ponla entre las dos marcas: en el archivo anterior antes de
   instalar (el instalador la conserva), o en el archivo nuevo después. Para ver cuáles de tus líneas no son del
   paquete, compara tu archivo con el del paquete, `Library/GPUBundles/nvmtl/nvrm610.conf` dentro de su `pkgroot`.
   Volver a 2.9 instalándola de nuevo también conserva solo ese bloque.

## 2. El primer arranque

1. **La tarjeta arrancó.** Instala y reinicia. Bien: la ventana de inicio de sesión aparece en la tarjeta NVIDIA;
   `kmutil showloaded --list-only | grep nullmoth` muestra las líneas de NVRM, NVAccel, NVRMFB y NVRMAGDC, y en
   macOS 26 también `com.nullmoth.NMXAudio`, el driver de audio que el instalador agrega allí para un controlador HD
   Audio;
   `system_profiler SPDisplaysDataType` muestra la GeForce con Metal soportado. Mal: no aparece la ventana de inicio
   de sesión (la pantalla conserva la última imagen del arranque, o queda en negro), o el Mac se reinicia solo y
   vuelve a arrancar con 2.9. En el primer caso busca, por `ssh`, en el registro del kernel `NOT STARTING THE GPU` (la
   línea da el motivo) y sigue con los pasos 3 y 5 de abajo, que distinguen los motivos. En el segundo,
   `cat /Library/NullMoth/lastgood.log` dice por qué la tarea del instalador devolvió 2.9 (el paso 3 explica el
   rechazo que puede nombrar); envía ese archivo con `sw_vers`. Una pantalla negra con solo el puntero, o una ventana
   de inicio de sesión que vuelve una y otra vez, es WindowServer cayéndose: `cat /Library/NullMoth/bootguard.log`
   por `ssh` lo dice (`WindowServer keeps dying ...`). El driver queda entonces apagado para el arranque siguiente, y
   la tarea del instalador, mientras siga ahí, devuelve 2.9 en ese arranque; el Mac se reinicia solo únicamente desde
   la ventana de inicio de sesión y únicamente tras la primera de esas caídas en serie, así que con una sesión abierta
   ejecuta `sudo shutdown -r now`. El mismo registro dice cómo se contó cada arranque que llegó a un escritorio:
   `... this start counts as good` termina la prueba de la instalación; `... with no NVIDIA driver running; the
   start count is cleared and the install's trial goes on` quiere decir que el driver no se ejecutó en ese arranque;
   `... the driver shows no picture and no other GPU has a display; this start keeps its count` quiere decir que NVRM
   arrancó la tarjeta y que 45 segundos después de aparecer WindowServer no había nada encendido (dos arranques así
   seguidos apagan el driver).
   En un Mac cuyo OpenCore no excluye IONDRVSupport de Apple (la tabla del README lo excluye) una ventana de inicio de
   sesión todavía no es un "bien": cuando el driver rechaza la tarjeta, el framebuffer de firmware de la propia Apple
   la toma y muestra una ventana de inicio de sesión y un escritorio lentos y sin aceleración. Dos órdenes de solo
   lectura distinguen los casos:

   ```bash
   ioreg -r -c IOFramebuffer -w0 | grep -o '<class [A-Za-z]*'
   cat /Library/NullMoth/lastgood.log
   ```

   Ejecútalas en el escritorio que apareció tras la instalación. La primera nombra `NVRMNVDAFramebuffer` cuando el
   driver dibuja la pantalla (bien) e `IONDRVFramebuffer` cuando lo hace el framebuffer de firmware de Apple (mal: el
   driver rechazó la tarjeta, el paso 3 dice por qué). La segunda dice qué hizo entonces la tarea del instalador. En
   un Mac así reinicia sola a 2.9 a los pocos segundos del arranque, también tras un inicio de sesión automático: el
   archivo termina con `previous driver restored; restarting`, o, si ya habías iniciado sesión en el escritorio lento
   de Apple, con `... the previous driver is back; restarting`. Tras ese reinicio la primera orden nombra el
   framebuffer de 2.9, así que lee el archivo. Solo cuando otra GPU muestra una pantalla y has iniciado sesión te deja
   a ti el reinicio (el archivo termina con `... so the Mac is not restarted now: the previous driver starts at the
   next restart`). Envía el archivo con `sw_vers`. Si la primera orden nombra `NVRMBootFB`, el driver está apagado en
   este arranque (dos arranques sin pantalla, las caídas en serie de WindowServer, `nullmoth-remove` o `-nvoff`) y no
   se devolvió nada: instala 2.9 de nuevo.
2. **Ningún interruptor olvidado.**
   `sysctl debug.nvrm_switches debug.nvrm_switches_set debug.nvrm_switches_nondefault`. Bien:
   `debug.nvrm_switches_nondefault` es 0. Mal: hay puesto un nombre marcado con `!`; quítalo de `boot-args` y
   reinicia antes de nada ([SWITCHES.md](SWITCHES.md) dice qué es).
3. **Las comprobaciones de las clases de Apple.** Nadie ha informado de una comparación de los tamaños de las clases de
   Apple para los que se hizo esta compilación con los que registra un macOS: en cada macOS que tengas (15 y 26) el
   primer arranque es esa comparación, y es la prueba que importa. Justo después (por `ssh` si la pantalla no apareció):

   ```bash
   ioreg -l -w0 | grep -E '"(nvrmfb|nvagdc|nvaccel|nvrm)-layout'
   sysctl debug.nvrmfb_layout debug.nvrmfb_layout_vtdiff debug.nvrmfb_agdc debug.nvrmfb_agdc_layout \
          debug.nvaccel_layout debug.nvaccel_layout_bad debug.nvaccel_layout_vtlong debug.nvaccel_layout_vtshort
   log show --last boot --predicate 'eventMessage CONTAINS "Apple class layout" OR eventMessage CONTAINS "Apple vtable lengths"'
   ```

   `sysctl` imprime `unknown oid` para un nombre que ningún kext registró, sigue con los demás y al final sale con el
   número de nombres desconocidos; el `log show` tarda cerca de un minuto. Lo que el código está escrito para mostrar:
   - Bien: `nvrmfb-layout` dice `ok: 3 classes, vtables not compared; macOS <compilación> (Darwin <versión>)`,
     `nvagdc-layout` `ok: 1 class, vtables not compared; macOS ...`, y `nvaccel-layout` empieza por `ok:` y el número
     de clases y de vtables que comparó; no hay línea `nvrm-layout` (NVRM solo la escribe cuando se niega a arrancar).
     `debug.nvrmfb_layout`, `debug.nvrmfb_agdc`, `debug.nvrmfb_agdc_layout` y `debug.nvaccel_layout` valen 1 y
     `debug.nvaccel_layout_bad` vale 0. Una línea `differ:` o `not compared:` en `nvrmfb-layout-vtables` o
     `nvaccel-layout-vtables`, o `debug.nvrmfb_layout_vtdiff`, `debug.nvaccel_layout_vtlong` o
     `debug.nvaccel_layout_vtshort` por encima de 0, no es un fallo (la longitud de una vtable se informa, nunca es
     motivo de rechazo), pero envíala con `sw_vers`.
   - Mal: una línea empieza por `refused:` (nombra la clase y los dos tamaños). Lo que hace cada rechazo:
     - `nvrmfb-layout` rechazado, leído por NVRM antes de arrancar la tarjeta: el registro del kernel tiene
       `NOT STARTING THE GPU: NVRMFB cannot drive a display on this macOS ...`, `nvrm-layout` dice `declined: ...`,
       y el driver no muestra nada: todos los framebuffers del driver están construidos sobre esas clases, también el
       que muestra un escritorio con el driver apagado. No aparece ningún escritorio donde OpenCore excluye
       IONDRVSupport; donde no lo excluye, el framebuffer de firmware de Apple muestra uno lento y sin aceleración
       (paso 1). El guardián de arranque no cuenta este arranque. La tarea
       del instalador de la sección 1, paso 1, busca el rechazo cada cinco segundos durante los primeros cuatro
       minutos del arranque; copia las líneas `nvrm-layout` y `nvrmfb-layout` en `/Library/NullMoth/lastgood.log` y,
       en la compilación de macOS en la que se instaló, devuelve el driver anterior y reinicia, así que vuelves a
       estar en 2.9 y ese archivo es lo que hay que enviar (solo cuando otra GPU muestra una pantalla y has iniciado
       sesión te deja a ti el reinicio, y el archivo lo dice; el framebuffer de firmware de Apple en esta tarjeta no
       detiene el reinicio, ni siquiera tras un inicio de sesión automático). Sin la tarea (una primera
       instalación, o cuando la tarea ya se borró sola en el arranque siguiente a uno que encendió la pantalla), o en
       otra compilación de macOS, no se devuelve nada, y cada arranque termina como este hasta que uses `nvlayout=0` o
       quites el driver. `debug.nvrmfb_layout` no existe (el framebuffer no arrancó).
     - El mismo rechazo leído solo después de que NVRM arrancara: `auto-go: NOT STARTING THE GPU: the framebuffer
       refused this macOS's classes after NVRM had started (...)` y `nvrm-layout` `declined late ...`. Tampoco aquí
       muestra nada el driver, este arranque sí cuenta, y la tarea del instalador hace lo mismo que arriba.
     - `nvaccel-layout` rechazado: `NVAccel: does not attach: ... the display runs without acceleration` y, de NVRM,
       `auto-go: the accelerator declined this macOS's classes ...`; `debug.nvrm_boot_heads_why` vale 6, no existe
       `debug.nvaccel_layout` ni hay `nvagdc-layout` (AppleGraphicsDeviceControl solo se crea para un acelerador que
       arrancó). El framebuffer enciende la pantalla, así que el guardián de arranque cuenta el arranque como bueno y la
       tarea del instalador no devuelve nada. Nadie ha informado todavía de un escritorio sin aceleración con este
       driver. Si la pantalla se queda negra o solo muestra el puntero mientras WindowServer se cae una y otra vez y
       todas las pantallas están en esta tarjeta, la tarea del guardián de arranque apaga el driver y reinicia una vez
       desde la ventana de inicio de sesión, y la tarea de vuelta atrás, mientras siga instalada, devuelve el driver
       anterior en ese arranque siguiente; `/Library/NullMoth/bootguard.log`, leído por `ssh`, lo dice. Con una pantalla
       en otra GPU la tarea del guardián de arranque no toca esas caídas.
     - `nvagdc-layout` rechazado: `debug.nvrmfb_agdc` vale -6 y `debug.nvrmfb_agdc_layout` 2; el escritorio funciona
       sin AppleGraphicsDeviceControl, y el arranque cuenta como bueno.

     Envía la salida de las tres órdenes con `sw_vers` (tras un rechazo del framebuffer que la tarea deshizo, envía
     en su lugar `/Library/NullMoth/lastgood.log`). Después arranca una vez con `nvlayout=0` solo para ver si
     funciona lo demás (la comprobación se sigue haciendo, y su línea empieza entonces por
     `overridden (nvlayout=0):`), y vuelve a quitarlo (TROUBLESHOOTING.md explica qué arriesga ese arranque). Si la
     tarea ya devolvió 2.9, eso significa instalar de nuevo con `nvlayout=0` ya puesto en los argumentos de arranque.
     Si ese arranque falla, ningún otro interruptor ayuda (con `nvlayout=0` también arranca el framebuffer que muestra
     la imagen del firmware con el driver apagado): quita `nvlayout=0` del `config.plist` desde fuera de macOS.
4. **Tiempo de arranque.** `sysctl debug | grep nvrm_boot_` da cada fase del arranque en milisegundos. Bien: el
   escritorio aparece no más tarde que con 2.9, y `debug.nvrm_boot_heads_why` no es 7 con una pantalla conectada.
   Mal: aparece más tarde, o 7 con una pantalla conectada; arranca una vez con `-nvrmoldwaits` y envía los dos juegos
   de números.
5. **DMA (VT-d).** `sysctl debug.nvrm_dma_mode` e `ioreg -l -w0 | grep nvrm-dma`, una vez, para que el informe diga
   qué modo funcionó. Bien: en una máquina sin VT-d (o con la tabla DMAR quitada) `debug.nvrm_dma_mode: 0` y
   `"nvrm-dma" = "none (bus address = physical address)"`: la capa de DMA deja pasar entonces cada dirección tal
   cual, como 2.9, y todos los demás contadores `debug.nvrm_dma_*` se quedan en 0 (`nvrm_dma_refused` también, salvo
   que el RM pida una asignación entre pares o de registros de un dispositivo, que se rechaza en todos los modos). Con
   VT-d activo, 1 o 2: sigue [DMA-IOMMU.md](DMA-IOMMU.md),
   "Checking a VT-d machine". Mal: 3 significa que el driver no arrancó la tarjeta detrás del mapeador (la línea
   `NVRM-xnu: DMA: refused:` dice por qué); envíala.
6. **Los ayudantes del RM.** Lee dos veces, con diez segundos de diferencia:

   ```bash
   sysctl debug.nvrm_wq_mode debug.nvrm_atomic_mode debug.nvrm_bh_thread debug.nvrm_rc_armed debug.nvrm_rc_ticks \
          debug.nvrm_nano_wait_timeouts debug.nvrm_nano_retired
   ```

   Bien: `nvrm_wq_mode` 1, `nvrm_atomic_mode` 1, `nvrm_bh_thread` y `nvrm_rc_armed` 1 con una tarjeta,
   `nvrm_rc_ticks` unos diez más la segunda vez, `nvrm_nano_wait_timeouts` y `nvrm_nano_retired` 0 (un espacio
   retirado es uno que el driver dejó de reutilizar porque una llamada de un temporizador podía seguir dentro). Mal:
   -1 en uno de los dos modos (un trabajador o una autoprueba no arrancó), 0 en una cuenta, o los ticks parados;
   envía los valores y las líneas
   `NVRM-xnu:` ([INTERRUPT-PATH-AND-RC-TIMER.md](INTERRUPT-PATH-AND-RC-TIMER.md) explica cada valor).
7. **La valla de superficies tras iniciar sesión.** Sigue el punto 1 de [SURFACE-FENCE.md](SURFACE-FENCE.md), "What to
   look at on the card". Bien: `debug.nvrm_fence_state` 1, `debug.nvrm_fence_accel` 1, `debug.nvrm_fence_map_fail` y
   `debug.nvrm_fence_map_mismatch` 0. Es la primera vez que un canal se registra en la valla. Una aplicación lanzada
   desde Terminal con `NVMTL_PROCESS_ENV=1` escribe `surface fences: kernel (NVRM fence v1, compositor)` en su
   `nvmtl.log`; `surface fences: off (...)` dice entre paréntesis por qué no se usa (TROUBLESHOOTING.md enumera los
   motivos). Mal: cualquier otro valor; envía las líneas `NVRM-xnu: fence:`.
8. **Canales.** `sysctl debug | grep nvrm_chan_`. Bien: todo 0. Mal: cualquier otra cosa en un escritorio que todavía
   no ha hecho nada; envíalo con `sysctl debug.nvrm_xid_count`.

## 3. El escritorio

1. **Grabar la pantalla: la primera prueba que solo una tarjeta puede hacer.** Graba la pantalla (o compártela)
   durante diez minutos mientras arrastras ventanas sobre un video en marcha y abres Mission Control. Una captura o
   una grabación espera ahora al trabajo de GPU de WindowServer sin tener el cerrojo del acelerador; si el código de
   Apple mantiene durante esa espera otro cerrojo que también necesita un hilo con el cerrojo del acelerador, el
   escritorio se congela para siempre. Bien: no se congela, y después `sysctl debug | grep nvaccel_pageoff` muestra
   `nvaccel_pageoff_unlocked_wait` 1, `nvaccel_pageoff_unlocked_waits` mayor que antes de la grabación,
   `nvaccel_pageoff_unlocked_standdown` 0 y `nvaccel_pageoff_unlocked_wait_max_us` no por encima de 2000000 (2 s)
   más allá del momento que cuesta recuperar el cerrojo. `nvaccel_pageoff_unlocked_already`, `_recopied` y
   `_changed` por encima de 0 no son un fallo: cada uno cuenta un page-off que, tras la espera, vio que el recurso
   había cambiado (otra copia, otra superficie u otra memoria, u otro bit de validez) y actuó según lo que vio. Mal: el
   escritorio se congela y sigue congelado. Por `ssh`
   ejecuta `sudo spindump` mientras está congelado (muestrea todos los procesos, WindowServer y kernel_task incluidos,
   y dice dónde escribió su archivo); después reinicia, arranca con `nvaccel_pageoff_locked=1` y repite la prueba. Un
   congelamiento que este argumento cura es el caso descrito aquí: envía el spindump,
   `sysctl debug | grep nvaccel_pageoff` y las líneas `NVAccel: page-off:` del kernel.
   `nvaccel_pageoff_unlocked_standdown` 1 también es un fallo (el camino se apagó solo; la línea `page-off:` dice
   por qué).
2. **Fotogramas.** Usa el escritorio un minuto, después pon un video 4K o un juego con la GPU ocupada, y captura
   mientras la GPU está cargada: puntos 2 a 4 de SURFACE-FENCE.md, "What to look at on the card". Bien: sin cortes
   (tearing), parpadeo ni fotogramas a medio dibujar; los contadores como dice esa lista
   (`debug.nvrm_fence_expired_gpu`, `debug.nvrm_fence_expired_cpu`, `debug.nvrm_fence_strikes` y
   `debug.nvrm_fence_owners_distrusted` 0; `debug.nvaccel_surface_fence_timeouts` 0). Mal: un fallo visible, o uno de
   esos contadores por encima de 0; arranca una vez con `nvrmfence=0` y di si desaparece.
3. **Un proceso que muere.** Punto 6 de la misma lista: `kill -9` a una aplicación Metal en mitad de un fotograma,
   después detén otra con `kill -STOP` y reanúdala con `kill -CONT`. Bien: el escritorio no se atasca; como mucho un
   tirón de hasta 100 ms por la detenida, que suma uno a `debug.nvrm_fence_expired_cpu` y uno a
   `debug.nvrm_fence_strikes` (aquí es lo esperado, a diferencia del punto 2). Mal: el escritorio se atasca, o
   `debug.nvrm_fence_state` pasa a 2.

### Superficies de dos planos

Con un video en marcha, captura la pantalla durante un minuto (compartir pantalla, una grabación o una aplicación de
captura). Primero comprueba que el camino se ofrece:
`sysctl debug.nvaccel_surf_planes debug.nvaccel_pageoff_unlocked_wait` (los dos a 1) e
`ioreg -l | grep NVAccelSurfPlanes` (1). Después de la captura:

```bash
sysctl debug | grep -E 'nvaccel_(surf_plane|surfvram_ranged|pageoff_ranged)'
```

Bien: la captura muestra los colores correctos y el fotograma actual; `nvaccel_surfvram_ranged` y
`nvaccel_pageoff_ranged` suben; los demás contadores `nvaccel_surfvram_ranged_*` y `nvaccel_pageoff_ranged_fail` se
quedan en 0 en una máquina sin VT-d (`nvaccel_pageoff_ranged_temp` por encima de 0 no es un fallo); ninguna línea
`planar surfaces: self-test FAILED` en el registro del sistema. Envía `nvaccel_surf_plane_validbits` (1 o 2) en
cualquier caso. Mal: un tono verde o morado, un fotograma viejo, fotogramas que alternan entre dos, uno de esos
contadores por encima de 0, o esa línea FAILED. Envía las líneas `NVAccel: surfvram` del kernel; después arranca una
vez con `nvaccel_surf_planes=0` y di si desaparece. [STUDIO-475-476.md](STUDIO-475-476.md) tiene las pruebas más
largas que miden este camino.

## 4. Juegos y aplicaciones

1. **Esperar a la GPU.** Ejecuta un juego o una aplicación que use mucho la GPU durante diez minutos, y después:

   ```bash
   sysctl debug.nvrm_evt_clients debug.nvrm_evt_slept debug.nvrm_evt_woken debug.nvrm_evt_timeouts \
          debug.nvrm_evt_capped debug.nvrm_evt_user_missed debug.nvrm_evt_user_fallbacks
   ```

   Bien: `nvrm_evt_clients` 2 o más (las aplicaciones duermen en el evento, no solo WindowServer); la mayoría de las
   esperas dormidas acaban despertadas; `nvrm_evt_user_missed` pequeño al lado de `nvrm_evt_woken`;
   `nvrm_evt_user_fallbacks` y `nvrm_evt_capped` 0; el uso de CPU de la aplicación menor que con 2.9 o igual, y los
   fotogramas por segundo iguales. Mal: más tirones o más CPU que con 2.9, o `nvrm_evt_user_fallbacks` por encima de
   0 (esto vale para diez minutos sin ningún cambio de pantalla; si entre medias la pantalla o el Mac se duermen, una
   subida suya seguida de nuevas subidas de `nvrm_evt_woken` es el proceso que vuelve al evento, no un fallo, pero no
   se conoce nada que provoque esa subida: repórtala diciendo qué hacía la máquina). Lanza la aplicación una vez desde
   Terminal con `NVMTL_PROCESS_ENV=1 NVRM_EVENT_WAIT=0` delante de su orden y compara
   ([EVENT-WAIT.md](EVENT-WAIT.md) explica cada número).
2. **Un fallo de GPU que la aplicación sobrevive.** Sigue [CHANNEL-RECOVERY.md](CHANNEL-RECOVERY.md), "What to look at
   on the machine", en su orden: primero una hora de uso normal con todos los contadores `debug.nvrm_chan_*` a 0,
   después los fallos inyectados en una aplicación Metal desechable, y WindowServer al final. Bien: lo que dice
   cada punto de allí (un tirón o un fotograma incorrecto, y la aplicación sigue; el escritorio y las demás
   aplicaciones no lo notan). Mal: la aplicación pierde su dispositivo, el escritorio se congela o
   `debug.nvrm_chan_gave_up` sube; envía los contadores, las líneas `nvrm:` de la salida de la aplicación y las líneas
   `NullMoth:` del registro del sistema. Quita después la línea `NVK_FAULT_INJECT` de `nvrm610.conf`.
3. **Memoria en una sesión larga.** Juega una hora a un juego que carga texturas sobre la marcha (un juego de mundo
   abierto) dos veces: una tal como queda instalado, otra con `NVMTL_IMAGE_GRACE=0` en tu bloque de `nvrm610.conf`.
   Lánzalo las dos veces desde Terminal con `NVMTL_PROCESS_ENV=1`, para que se escriba su `nvmtl.log`. Compara la
   memoria del proceso en Monitor de Actividad a los 5, 30 y 60 minutos. Bien: con la gracia puede quedar unas
   cuantas texturas de fotogramas por encima, pero no sigue creciendo donde la prueba sin ella se mantiene plana; las
   líneas `vk: image grace:` de `nvmtl.log` muestran un número retenido pequeño que vuelve cerca de 0 cuando el juego
   está en reposo. Mal: la memoria sigue creciendo con la gracia y no sin ella, o una línea dice que se liberaron
   imágenes muertas 1024 envíos después de su muerte. Envía `nvmtl.log`. Aquí se aplican tres límites de esta
   versión (CHANGELOG.md, "Known limits of what is on"): no se libera nada antes cuando la aplicación llega a su
   límite de memoria; una aplicación que deja un búfer de órdenes sin confirmar (commit) retiene toda textura liberada
   después de él hasta que lo confirma o lo descarta, o hasta 1024 envíos después; la ranura bindless de una textura
   liberada sigue la regla anterior.
4. **Dibujo.** Juega a los juegos y usa las aplicaciones que usabas con 2.9, en RTX 50 sobre todo (el compilador de
   shaders lleva las correcciones posteriores de Mesa). Bien: la misma imagen que con 2.9 o mejor; los shaders se
   compilan otra vez una vez, así que el primer arranque de cada uno tarda más. Mal: un fallo de dibujo que 2.9 no
   tenía; envíalo con el nombre del juego y la tarjeta. Si compilas desde el código fuente, `NVK_SKIP_PATCHES` compila
   NVK sin uno de los parches del compilador para encontrar cuál lo causa
   ([NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md)).
5. **OpenGL (macOS 15.5 o posterior).** El OpenGL nativo a través de Zink aún no se ha ejecutado en un Mac con este
   árbol, y la disposición de GLEngine en la que se apoya se registró solo en macOS 15.8.1 (24H32) y 26.7.1 (25G241).
   Primero mira qué renderizador nombró el acelerador:

   ```bash
   log show --last boot --predicate 'eventMessage CONTAINS "NVAccel: OpenGL:"'
   ```

   Nombra `NVKZinkGLDriver (Zink on NVK)`, o `AppleMetalOpenGLRenderer` con `nvzink=0` o antes de macOS 15.5. Luego
   abre una aplicación OpenGL desde Terminal (un juego que use OpenGL, o Minecraft con su renderizador OpenGL) y
   guarda lo que escribe. Bien: dibuja como con 2.9 y no escribe ninguna línea que empiece por `NVKZinkGLDriver:` ni
   por `NVK Zink:`. Una línea `NVK Zink: OpenGL stays Apple's; GLEngine's private layout does not match the record
   (...)` quiere decir que el adaptador encontró distinta la disposición de esta compilación y dejó un contexto OpenGL
   de la aplicación en el OpenGL de Apple, como debe. La línea se escribe solo para el primer contexto rechazado en
   un proceso, y otros contextos de la aplicación pueden seguir siendo de Zink. En dos casos el contexto para el que
   se escribió la línea también es de Zink: la línea vino de un segundo motor GLI (el objeto de GLEngine para un
   renderizador del contexto) de un contexto que Zink ya atiende, y las llamadas hechas con ese motor se descartan,
   con la última línea de más abajo; o un motor GLI posterior del contexto rechazado pasó su propia comprobación y
   Zink tomó el contexto con él ([ZINK-OPENGL.md](ZINK-OPENGL.md), "The layout guard"). Envía esa línea y la salida
   de `sw_vers`, y di si la aplicación dibuja como debe. Una línea `NVK Zink: CGLTexImageIOSurface2D in a Zink
   context stays Apple's; GLEngine's private layout does not match the record (...)` quiere decir que Zink
   está dibujando y que solo esa llamada quedó con el manejador de Apple; donde aparece, la escribe toda aplicación
   OpenGL que Zink atiende, use o no esa llamada: envíala igual y di si algo de la aplicación se dibuja mal. Una
   línea `NVK Zink: a call through a Zink context was dropped; it did not come with the GLI engine Zink adopted for
   the current CGL context (...)` quiere decir que Zink tomó un contexto OpenGL de la aplicación y descartó después
   una llamada que venía para él; la línea se escribe solo para la primera llamada descartada. Donde se descartan
   todas las llamadas del contexto, ninguna devuelve nada, así que la aplicación puede mostrar una ventana vacía,
   decir que OpenGL no está disponible o cerrarse al abrir. Envía esa línea y la salida de `sw_vers`, di qué hizo la
   aplicación, y reinicia con el argumento de arranque `nvzink=0`, que devuelve a todas las aplicaciones el OpenGL de
   Apple. Mal: un cierre inesperado, una ventana negra o una imagen equivocada; reinicia una vez con el argumento de
   arranque `nvzink=0` y di si desaparece.
   Ver [ZINK-OPENGL.md](ZINK-OPENGL.md). Para volver a la versión 2.9 o a un paquete 3.0 anterior, ejecuta primero el
   `sudo ./uninstall.sh` de este paquete: los instaladores de esos paquetes no conocen
   `/Library/GPUBundles/NVKZinkGLDriver.bundle` y lo dejan en su sitio. Su acelerador no lo nombra, así que no se
   carga, y `sudo rm -rf /Library/GPUBundles/NVKZinkGLDriver.bundle` lo quita.

## 5. Video

1. **Qué decodificador hizo los fotogramas (RTX 50).** Reproduce un video H.264 en Safari, y después:

   ```bash
   log show --last 10m --predicate 'eventMessage CONTAINS "decode self-test"'
   ```

   Un video que ya estaba empezando mientras la tarjeta hacía la prueba (normalmente el primero después de reiniciar)
   puede seguir con el decodificador integrado: recárgalo una vez antes de juzgar. El servicio de decodificación
   (`VTDecoderXPCService`, que decodifica para Safari, QuickTime y FaceTime) pide el decodificador H.264 de la
   biblioteca de códecs solo después de que la tarjeta haya decodificado dos flujos de prueba integrados y cada
   fotograma coincidiera bit a bit, una vez por cada proceso del servicio.
   - Bien: una línea `NullMoth: nvencvt:` con `decode self-test: PASS` (dice cuánto tardó la prueba), y el video se ve
     con los colores correctos.
   - Mal: `decode self-test: FAIL at <step>`. Entonces el decodificador integrado es el único, como en 2.9 (verde en
     RTX 50); el paso dice hasta dónde llegó la tarjeta. Una prueba fallida puede dejar un Xid en el registro del
     kernel; es su rastro, no un segundo fallo. Envía la línea.
   - Mal: una línea que dice que la biblioteca de códecs instalada es una compilación sin la autoprueba de
     decodificación Blackwell y que se rechazó (REFUSED) en esta tarjeta: la biblioteca es más antigua que el plugin
     que tiene al lado. Compila las dos de la misma revisión e instala las dos.
   - Ninguna línea así (`log show` imprime entonces solo su encabezado y una línea que es la propia orden `log`, que
     coincide con su propio filtro): el proceso no lo pidió nunca (el driver no informa la generación de la tarjeta
     como Blackwell, está puesto `NVMTL_NVENCVT=0`, o la biblioteca no está instalada con root como propietario), o
     todavía no ha arrancado ningún servicio de decodificación.
   - PASS pero un video roto: pon `NVMTL_NVENCVT=0` en `nvrm610.conf` para volver al decodificador de 2.9, y envía la
     línea con los datos del video.

   Para el registro propio de la biblioteca, `sudo touch /tmp/nvencvt.log` antes de que arranque el proceso; cada
   proceso que carga la biblioteca escribe entonces `/tmp/nvencvt-<pid>.log`. El servicio de decodificación funciona
   en un sandbox y puede que no se le permita: si no aparece ese archivo para él, eso no dice nada de la prueba, cuyo
   veredicto es la línea del registro del sistema de arriba.
2. **RTX 30 y 40.** Reproduce videos H.264, HEVC, VP9 y AV1 en Safari y QuickTime. Bien: como con 2.9. Mal: un video
   que se veía con 2.9 no se ve; envía los datos del video y
   `log show --last 10m --predicate 'eventMessage CONTAINS "nvencvt"'`.

## 6. Reposo y activación

1. Pon el Mac en reposo desde el menú Apple, espera un minuto y actívalo. Hazlo tres veces, una con un video en marcha
   y otra con un juego abierto. Bien: el escritorio vuelve cada vez; `sysctl debug.nvrm_fence_state` es 1 después
   (`debug.nvrm_fence_end_pm` cuenta las retenciones que estaban en curso en el momento de cada reposo, que pueden ser
   ninguna, así que puede quedarse igual); `debug.nvrm_irq_settle_timeouts` 0; el registro del kernel tiene una línea
   `NVRM-xnu: sleep #` y una `NVRM-xnu: wake #` por cada vez; ninguna aplicación perdió su GPU y ningún fotograma
   pendiente en el reposo se dio por perdido. Con VT-d activo, `debug.nvrm_dma_live` da lo mismo antes y después de
   un reposo sin nada más en marcha. Mal: pantalla negra o congelada tras la activación, `debug.nvrm_fence_state` 3
   (la GPU no se reanudó), o una aplicación que perdió su dispositivo; envía las líneas `NVRM-xnu:` y `NVRM-fb:` de
   ese arranque ([SLEEP-WAKE.md](SLEEP-WAKE.md) las enumera).

## 7. Dos GPU

Solo para una máquina con dos tarjetas NVIDIA. Prepárala primero como describe [MULTI-GPU.md](MULTI-GPU.md), para que
el driver use la tarjeta a la que está conectada la pantalla, y repite las secciones 2 y 6. Bien: todo como con una
tarjeta, y el reposo y la activación devuelven el escritorio. Después, si quieres probar el modo experimental en el
que se acelera cada tarjeta, arranca con `nvmultigpu=1` ([MULTI-GPU-DESIGN.md](MULTI-GPU-DESIGN.md)) y repite las dos
secciones. Bien: `debug.nvrm_bh_thread` y `debug.nvrm_rc_armed` dan 2; el arranque de cada tarjeta está en el
registro del kernel; tras una activación vuelven las dos tarjetas y `debug.nvrm_fence_state` es 1. Mal: falta una
tarjeta en esos contadores, o una activación pierde una; envía el registro del kernel de ese arranque.

## Comprobaciones automáticas en la tarjeta (tools/card_selftest.py)

`tools/card_selftest.py` ejecuta las comprobaciones en la tarjeta de las partes nuevas (Vulkan nativo, OpenGL nativo
con Zink, el plugin de Metal y el traductor) y de los cambios propios de la versión 3.0.1 en una sola pasada sin
atención, en el Mac con el paquete instalado y su driver cargado. Se ha ejecutado una vez en una tarjeta, en macOS
26.7.3 (25G342) con el paquete de la versión 3.0.1. Desde entonces se añadió `install.plugin-translator`, se reescribió
el caso `abarray-c` de `xlate.paths-abarray`, que el compilador de Apple rechazó allí, y `plugin.photobooth-surface`,
que no se compiló allí, enlaza ahora CoreGraphics: estas todavía no se han ejecutado en una tarjeta. Su prueba de host
`tools/test_card_selftest.py` solo se ha ejecutado en Linux.

Hay 134 comprobaciones en seis partes: `install`, `xlate`, `plugin`, `vulkan`, `zink` y `v301`. Las diez de `v301`
(`tools/card/checks_v301.py`) comprueban los cambios de la versión 3.0.1: la subida asíncrona de replaceRegion y la
traducción en segundo plano de los shaders de un juego, las dos activas por defecto desde la 3.0.1, cada una junto a su
vuelta atrás (`NVMTL_ASYNC_TEX_UPLOAD=0`, `NVMTL_PRETRANSLATE=0`); shaders, texturas y samplers nuevos usados enseguida
con la memoria mapeada propia de NVK donde la pone la 3.0.1 y con `NVK_LOCAL_MAP_SYSMEM=1`; la retirada de
`VK_EXT_map_memory_placed` e hilos que mapean y liberan memoria a la vez con el runtime de Vulkan nativo; y una lectura
de la política de boost como la hace NVK. Cada una necesita un archivo que añadió la versión 3.0.1, así que en un
checkout sin la 3.0.1 cada una se omite y nombra ese archivo, y una comprobación de un valor por defecto también se
omite cuando `nvrm610.conf` fija ese interruptor. Las diez se ejecutaron en esa pasada.

Las ocho de `install` (`tools/card/checks_install.py`) no tocan la GPU: seis leen los archivos instalados (cada archivo
frente al paquete preparado, el bundle de OpenGL que nombra el acelerador, las carpetas de Vulkan nativo y de OpenGL
nativo, el commit de Mesa con el que se compiló OpenGL nativo y el traductor que abre el plugin de Metal) y dos ejecutan
las sondas instaladas de OpenGL nativo. La del traductor, `install.plugin-translator`, existe porque el plugin abre el
`libnvmtl_translate.dylib` que tiene al lado en `NVMTLDriver.bundle/Contents/MacOS` siempre que ese archivo está, y
`nvmtl/libnvmtl_translate.dylib` solo si no, mientras que las comprobaciones de lectura de `xlate` traducen
directamente con la copia de `nvmtl/`: un paquete preparado sobre otro más antiguo puede conservar la copia antigua en
el bundle, y entonces las comprobaciones de Metal prueban otro traductor que las de lectura. Falla cuando la copia que
abre el plugin no es la misma compilación que la de `nvmtl/` (por su LC_UUID) o no es Mach-O. Si no encuentra esa
preferencia en el `plugin/NVMTLLibraryLoad.m` del checkout, falla siempre que haya junto al plugin una copia legible
que no sea esa compilación, porque todas las versiones de la 1.0.0 a la 3.0.1 abren esa copia. Da el LC_UUID de cada
copia, escrito como lo escribe el log del plugin, su tamaño y el principio de su SHA-256.

Arráncalo desde el checkout con el que se compiló el paquete, en segundo plano, y espera a que termine:

```
mkdir -p ~/nullmoth-claude/cardtest
cd ~/nullmoth-claude/src
/usr/bin/python3 -B -I tools/card_selftest.py --out ~/nullmoth-claude/cardtest/run1 \
  > ~/nullmoth-claude/cardtest/run1.txt 2>&1 &
```

Su carpeta tiene entonces `DONE`, cuya primera línea es el código de salida, y `report.md`, a su lado, es la respuesta.
Solo un rechazo antes de crear la carpeta (una carpeta de salida o un archivo de comprobaciones equivocados, salida 3) o
una opción equivocada (salida 2) no deja `DONE`; lo dice el archivo de salida. `--list` muestra cada comprobación y no
compila nada; `--plan` mira las condiciones del párrafo siguiente, todas menos la sonda de Metal, y muestra qué se
compilaría y ejecutaría, sin ningún programa de GPU; `--no-window` deja fuera las comprobaciones que abren una ventana;
`--only REGEX` ejecuta solo las comprobaciones cuyo id coincide; `--expect COMMIT` da el commit del paquete instalado
cuando no es el de por defecto. El de por defecto es el commit de la versión 3.0: para un paquete de la versión 3.0.1
o posterior, da el commit con el que se compiló.

Antes de compilar ninguna comprobación rechaza la pasada (salida 3) salvo que la carpeta de salida esté dentro de
`~/nullmoth-claude`, fuera de `src` y de `build2`, vacía o inexistente y con 2 GB libres; el checkout esté limpio y sus
fuentes del driver sean las del commit esperado; el paquete preparado en `~/nullmoth-claude/build2/package/pkgroot` sea
de ese commit y cada archivo que pone en `/Library/GPUBundles` esté instalado sin cambios (`nvrm610.conf` no se
compara); el acelerador esté en el IORegistry; cuando el checkout tiene la política de boost de la versión 3.0.1, el
NVRM.kext cargado responda a `debug.nvrm_boost_policy` (los kexts de un paquete anterior siguen cargados hasta un
reinicio); y una pequeña sonda vea el dispositivo Metal del driver. Un rechazo de la carpeta de salida no escribe nada;
cualquier rechazo posterior escribe `report.md` y `DONE` con el motivo.

Solo escribe en la carpeta de salida (por defecto `~/nullmoth-claude/cardtest/<hora UTC>`): `report.md` (los datos de la
máquina, entre ellos la política de boost y el borrado con el motor de copia de la memoria de vídeo nueva al principio y
al final, y una línea por comprobación), `results.json` (que también se escribe después de cada comprobación, marcado
como parcial, para que un programa matado sin su informe deje las comprobaciones que terminó), `logs/` (un registro por
comprobación y por compilación, con la
salida del programa y su `nvmtl.log`), `bin/` (las comprobaciones compiladas), `tmp/` y `cache/` (el TMPDIR de cada
comprobación y las cachés de shaders a las que se las dirige), y `DONE` al final. En `report.md`, `results.json`, los
registros y cada línea que muestra, la carpeta personal pasa a ser `~`, el nombre de la cuenta `<user>`, el nombre del
equipo `<host>` y las carpetas temporal y de caché del usuario `<usertmp>` y `<usercache>`. `report.md` termina con lo
que apareció fuera de la carpeta mientras se ejecutaba, solo por nombre: archivos del plugin y registros de diagnóstico
de NVK en `/tmp`, entradas nuevas de las cachés de shaders, informes de fallo de los programas que ejecutó.

Código de salida: 0 pasó cada comprobación que se ejecutó (las omitidas y las inactivas aparecen con su motivo); 1 una
comprobación falló, se quedó sin tiempo, se cayó, no se pudo parar o no compiló; 2 una opción equivocada; 3 rechazada; 4
parada antes de tiempo (también si antes falló una comprobación, si se interrumpió o si falló el propio programa). Cada
comprobación tiene un límite de tiempo. Una comprobación con un proceso que sigue vivo 15 s después de matar su grupo de
procesos (en su límite o al terminar), una comprobación de GPU que se quedó sin tiempo, una subida de
`debug.nvrm_chan_gave_up`, un cambio de `debug.nvrm_fence_state`, un proceso de WindowServer nuevo, una subida de
`debug.nvrmfb_vram_ce_kept` (no se vio terminar el borrado con el motor de copia de un bloque nuevo de memoria de
vídeo), un cambio de `debug.nvrmfb_vram_ce_clear` desde 1 (el borrado con el motor de copia se retiró hasta el próximo
arranque, por ejemplo porque un bloque que dijo haber borrado no se leía como cero) o una subida de
`debug.nvrmfb_vram_power_events` (la GPU pasó por un reposo) paran todas las comprobaciones de GPU que vienen después.
Una subida de `debug.nvrm_xid_count`, de `debug.nvrm_chan_recovered` (un canal que NVK reconstruyó) o de
`debug.nvrm_chan_dropped` (los envíos que una reconstrucción descartó) hace fallar la comprobación en la que ocurrió, y
la segunda comprobación de GPU con una subida así para todas las que vienen después; un canal atascado al que se volvió
a llamar (`debug.nvrm_chan_kicks`) se anota en la comprobación. Pasados 40 minutos (`--deadline`) no empieza nada nuevo.
Una comprobación que no se arrancó aparece como "not run" con el motivo.

Nunca usa sudo; no instala, carga ni copia nada en `/Library` ni en `/System`; no cambia ningún ajuste (ni
`nvrm610.conf`, ni `defaults`, ni un argumento de arranque, ni la política de boost); no ejecuta `nvram`, `csrutil`,
`bless`, `kmutil`, `kextload` ni `launchctl`; ejecuta cada comprobación con `NVK_DIAG=0`, que gana a una línea
`NVK_DIAG=1` de `nvrm610.conf` (NVK escribiría en `/private/tmp`), y no deja que ninguna comprobación lo ponga; no lee
la colección de kernels, el binario del kernel, la caché compartida de dyld ni el binario de un framework; no abre
ninguna conexión de red; no pide ningún permiso (cámara, micrófono, grabación de pantalla, accesibilidad,
automatización, archivos de Escritorio, Documentos o Descargas); y solo mata los grupos de procesos de sus propios
hijos. Para las comprobaciones con ventana se abren ventanas un momento; `caffeinate` mantiene la pantalla despierta
solo mientras se ejecutan.

## Las vueltas atrás

Una por cambio; cada una devuelve el comportamiento de 2.9 solo de ese cambio. Están para saber si un cambio está
detrás de un problema, no para dejarlas puestas. Un argumento de arranque actúa en el siguiente reinicio: los cuatro
kexts no se pueden descargar y volver a cargar a mano, porque cada uno se mantiene cargado desde que arrancó
(`kmutil unload` lo rechaza hasta el siguiente reinicio).

| Cambio | Vuelta atrás | Dónde |
|---|---|---|
| Valla de superficies | `nvrmfence=0` (la sincronización de superficies es la de 2.9; la valla sigue respondiendo a un proceso que pregunta si está activa, y NVK sigue abriendo su conexión) | argumento de arranque |
| Espera de page-off sin el cerrojo del acelerador | `nvaccel_pageoff_locked=1` | argumento de arranque |
| Superficies de dos planos en VRAM | `nvaccel_surf_planes=0` | argumento de arranque |
| Comprobaciones de las clases de Apple | `nvlayout=0` | argumento de arranque |
| Esperas del arranque | `-nvrmoldwaits` | argumento de arranque |
| Colas de trabajo del RM | `nvrmworkq=0` | argumento de arranque |
| Respuestas según el contexto | `nvrmatomic=0` | argumento de arranque |
| Hilo de la mitad inferior (bottom half) | `nvrmbhthread=0` | argumento de arranque |
| Temporizador RC | `nvrmrctimer=0` | argumento de arranque |
| Espera al cancelar un temporizador nano | `nvrmnanowait=0` | argumento de arranque |
| DMA a través de VT-d | `nvrmdma=0` (con un mapeador la tarjeta no arranca; sin él no cambia nada) | argumento de arranque |
| Espera por evento | `NVRM_EVENT_WAIT=0` (todo proceso sondea, WindowServer incluido) | `nvrm610.conf` |
| Recuperación de canales | `NVK_CHANNEL_RECOVER=0` | `nvrm610.conf` |
| Informe de error del trabajo descartado | `NVMTL_DROP_REPORT=0` | `nvrm610.conf` |
| Gracia de imágenes | `NVMTL_IMAGE_GRACE=0` | `nvrm610.conf` |
| Decodificación H.264 de RTX 50 en el servicio de decodificación | `NVMTL_NVENCVT=0` (la biblioteca de códecs apagada en todas partes) | `nvrm610.conf` |
| Texturas de video 4:2:2 (fotogramas de cámara) | `NVMTL_YUV422=0` (las texturas 4:2:2 se rechazan) | `nvrm610.conf` |
| OpenGL nativo a través de Zink | `nvzink=0` (el OpenGL de Apple sobre Metal para todas las aplicaciones) | argumento de arranque |
| Correcciones del compilador de NVK | `NVK_SKIP_PATCHES="<nombres de parches>"` | al compilar desde el código fuente |

CHANGELOG.md, "The ways back", y SWITCHES.md dicen qué devuelve cada una en detalle.

## Qué adjuntar a un informe

- `sw_vers`, la tarjeta y `nvram boot-args`.
- `sysctl debug.nvrm_switches debug.nvrm_switches_set debug.nvrm_switches_nondefault`.
- Tu bloque de `/Library/GPUBundles/nvmtl/nvrm610.conf`.
- `sysctl debug | grep -e nvrm -e nvaccel`, tomado mientras o justo después de que aparezca el problema, y lo mismo
  de 2.9 (`~/before-3.0.txt`).
- El registro del kernel del arranque: `log show --last boot --predicate 'process == "kernel"' > ~/kernel.txt`.
- Las líneas de fallo del plugin:
  `log show --last boot --predicate 'eventMessage CONTAINS "NullMoth:"' > ~/nullmoth.txt`.
- Para una aplicación lanzada con `NVMTL_PROCESS_ENV=1`: su `nvmtl.log`, y las líneas `nvrm:` que imprimió en
  Terminal.
- Qué sección y qué punto de arriba fallaron, y si su vuelta atrás hizo desaparecer el problema.
