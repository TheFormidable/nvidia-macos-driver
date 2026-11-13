# Probar la versión 3.0 en una tarjeta

Mientras [README.md](../README.md) empiece con el aviso "Work in progress: do not install this branch yet" (trabajo
en curso: no instales todavía esta rama), aquí no hay nada que instalar: esta página es la lista de comprobaciones
para el día en que el aviso desaparezca, y hasta entonces es solo para leer. Nada de lo que contiene se ha visto en
una tarjeta NVIDIA. Cada "bien" de abajo es lo que el código está escrito para mostrar, no un resultado que alguien
haya observado.

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
  registro del sistema.
- **NVK** (la capa Vulkan bajo el plugin) imprime sus líneas, que empiezan por `nvrm:`, en la salida de error de la
  aplicación: lanza la aplicación desde Terminal para verlas.

Los ajustes de `/Library/GPUBundles/nvmtl/nvrm610.conf` van en tu propio bloque al final del archivo, entre
`# --- my settings, kept on update ---` y `# --- end my settings ---` (la sección 1, paso 5, dice por qué y qué
conserva una instalación); gana la última línea de cada nombre, y un
proceso lee el archivo al arrancar (reinicia la aplicación, o cierra sesión y vuelve a entrar para WindowServer). Los
argumentos de arranque van en la línea `boot-args` del `config.plist` de OpenCore (README.md, "Wiring into an existing
OpenCore setup") y necesitan reiniciar. Quita cada ajuste de prueba cuando termines su prueba.

## 1. Antes de instalar

1. **Una vuelta atrás que hayas probado.** Ten a mano el paquete descomprimido de la versión sobre la que instalas
   (2.9): instalarlo de nuevo con su propio `install.sh` devuelve cada archivo a su sitio
   ([BUILDING.md](BUILDING.md), "Installing the result and going back", que también dice qué hace y qué no hace
   `uninstall.sh`). Antes de cambiar nada, el instalador copia lo que está instalado ahora (las kexts, el bundle del
   plugin, la carpeta `/Library/GPUBundles/nvmtl` con su archivo de ajustes, la colección del kernel, el firmware) en
   una carpeta nueva `/Library/NullMoth/backup-<fecha>-<hora>.<aleatorio>`, cuyo nombre imprime en su línea
   `== 4. back up what is there now -> ...`; anótalo. También deja una tarea que se ejecuta en cada arranque: cuando el
   driver nuevo se apagó solo tras dos arranques seguidos que nunca encendieron su pantalla (o tras caerse
   WindowServer una y otra vez en la pantalla NVIDIA), la tarea devuelve todo desde esa carpeta y reinicia una vez. Se
   borra sola en el primer arranque después de uno que encendió la pantalla, así que no ayuda con un problema que
   aparezca más tarde. Ten claro cómo arrancarás el Mac si el escritorio no aparece
   ([TROUBLESHOOTING.md](TROUBLESHOOTING.md), "4. Getting back in"; las entradas de 1401 en el selector de arranque
   solo existen en un Mac preparado con la app 1401). Activa Sesión remota (Configuración del Sistema, o Ajustes del
   Sistema según el idioma, General, Compartir) y comprueba que llegas al Mac por `ssh` desde otra computadora: varios
   fallos de abajo dejan la imagen del firmware
   o un escritorio congelado, y entonces los registros se leen por la red.
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
4. **El paquete.** Desde su `pkgroot`, `sudo env CHECK=1 ./install.sh` comprueba el paquete y el Mac y no cambia
   nada. Bien: su última línea es `CHECK PASS (nothing changed)`. Mal: una línea `STOP:` dice qué rechazó; arréglalo
   primero.
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
   `kmutil showloaded --list-only | grep nullmoth` muestra cuatro líneas (NVRM, NVAccel, NVRMFB, NVRMAGDC), cinco
   en macOS 26, donde el instalador agrega su driver de audio `com.nullmoth.NMXAudio` para un controlador HD Audio;
   `system_profiler SPDisplaysDataType` muestra la GeForce con Metal soportado. Mal: se queda la imagen del firmware o la pantalla en negro. Por `ssh`,
   busca en el registro del kernel `NOT STARTING THE GPU` (la línea da el motivo) y sigue con los pasos 3 y 5 de
   abajo, que distinguen los motivos.
2. **Ningún interruptor olvidado.**
   `sysctl debug.nvrm_switches debug.nvrm_switches_set debug.nvrm_switches_nondefault`. Bien:
   `debug.nvrm_switches_nondefault` es 0. Mal: hay puesto un nombre marcado con `!`; quítalo de `boot-args` y
   reinicia antes de nada ([SWITCHES.md](SWITCHES.md) dice qué es).
3. **Las comprobaciones de las clases de Apple.** Nadie ha comparado los tamaños de las clases de Apple para los que
   se hizo esta compilación con los que registra tu macOS: en cada macOS que tengas (15 y 26) el primer arranque es
   esa comparación, y es la prueba que importa. Justo después (por `ssh` si la pantalla no apareció):

   ```bash
   ioreg -l -w0 | grep -E '"(nvrmfb|nvagdc|nvaccel|nvrm)-layout'
   sysctl debug.nvrmfb_layout debug.nvrmfb_layout_vtdiff debug.nvrmfb_agdc debug.nvrmfb_agdc_layout \
          debug.nvaccel_layout debug.nvaccel_layout_bad debug.nvaccel_layout_vtlong debug.nvaccel_layout_vtshort
   log show --last boot --predicate 'eventMessage CONTAINS "Apple class layout" OR eventMessage CONTAINS "Apple vtable lengths"'
   ```

   `sysctl` imprime `unknown oid` para un nombre que ninguna kext registró, sigue con los demás y al final sale con el
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
       se queda la imagen del firmware y el guardián de arranque no cuenta este arranque. `debug.nvrmfb_layout` no
       existe (el framebuffer no arrancó).
     - El mismo rechazo leído solo después de que NVRM arrancara: `auto-go: NOT STARTING THE GPU: the framebuffer
       refused this macOS's classes after NVRM had started (...)` y `nvrm-layout` `declined late ...`. Se queda la
       imagen del firmware y este arranque sí cuenta: dos arranques así seguidos sin un escritorio que se mantenga
       apagan el driver, y la tarea del instalador de la sección 1, paso 1, devuelve el driver anterior.
     - `nvaccel-layout` rechazado: `NVAccel: does not attach: ... the display runs without acceleration` y, de NVRM,
       `auto-go: the accelerator declined this macOS's classes ...`; `debug.nvrm_boot_heads_why` vale 6, no existe
       `debug.nvaccel_layout` ni hay `nvagdc-layout` (AppleGraphicsDeviceControl solo se crea para un acelerador que
       arrancó). El framebuffer enciende la pantalla, así que el guardián de arranque cuenta el arranque como bueno y
       la tarea del instalador no devuelve nada. Nadie ha visto todavía un escritorio sin aceleración con este driver:
       si la pantalla se queda negra o solo muestra un cursor, usa `ssh`.
     - `nvagdc-layout` rechazado: `debug.nvrmfb_agdc` vale -6 y `debug.nvrmfb_agdc_layout` 2; el escritorio funciona
       sin AppleGraphicsDeviceControl, y el arranque cuenta como bueno.

     Envía la salida de las tres órdenes con `sw_vers`. Después arranca una vez con `nvlayout=0` solo para ver si
     funciona lo demás (la comprobación se sigue haciendo, y su línea empieza entonces por
     `overridden (nvlayout=0):`), y vuelve a quitarlo (TROUBLESHOOTING.md explica qué arriesga ese arranque).
4. **Tiempo de arranque.** `sysctl debug | grep nvrm_boot_` da cada fase del arranque en milisegundos. Bien: el
   escritorio aparece no más tarde que con 2.9, y `debug.nvrm_boot_heads_why` no es 7 con una pantalla conectada.
   Mal: aparece más tarde, o 7 con una pantalla conectada; arranca una vez con `-nvrmoldwaits` y envía los dos juegos
   de números.
5. **DMA (VT-d).** `sysctl debug.nvrm_dma_mode`. Bien: 0 en una máquina sin VT-d (o con la tabla DMAR quitada), y
   todos los demás contadores `debug.nvrm_dma_*` a 0. Con VT-d activo, 1 o 2: sigue [DMA-IOMMU.md](DMA-IOMMU.md),
   "Checking a VT-d machine". Mal: 3 significa que el driver no arrancó la tarjeta detrás del mapeador (la línea
   `NVRM-xnu: DMA: refused:` dice por qué); envíala.
6. **Los ayudantes del RM.** Lee dos veces, con diez segundos de diferencia:

   ```bash
   sysctl debug.nvrm_wq_mode debug.nvrm_atomic_mode debug.nvrm_bh_thread debug.nvrm_rc_armed debug.nvrm_rc_ticks \
          debug.nvrm_nano_wait_timeouts
   ```

   Bien: `nvrm_wq_mode` 1, `nvrm_atomic_mode` 1, `nvrm_bh_thread` y `nvrm_rc_armed` 1 con una tarjeta,
   `nvrm_rc_ticks` unos diez más la segunda vez, `nvrm_nano_wait_timeouts` 0. Mal: -1 en uno de los dos modos (un
   trabajador o una autoprueba no arrancó), 0 en una cuenta, o los ticks parados; envía los valores y las líneas
   `NVRM-xnu:` ([INTERRUPT-PATH-AND-RC-TIMER.md](INTERRUPT-PATH-AND-RC-TIMER.md) explica cada valor).
7. **La valla de superficies tras iniciar sesión.** Sigue el punto 1 de [SURFACE-FENCE.md](SURFACE-FENCE.md), "What to
   look at on the card". Bien: `debug.nvrm_fence_state` 1, `debug.nvrm_fence_accel` 1, `debug.nvrm_fence_map_fail` y
   `debug.nvrm_fence_map_mismatch` 0. Es la primera vez que un canal se registra en la valla. Mal: cualquier otro
   valor; envía las líneas `NVRM-xnu: fence:`.
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
   más allá del momento que cuesta recuperar el cerrojo. Mal: el escritorio se congela y sigue congelado. Por `ssh`
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
   tirón de hasta 100 ms por la detenida. Mal: el escritorio se atasca, o `debug.nvrm_fence_state` pasa a 2.

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
   0. Lanza la aplicación una vez desde Terminal con `NVMTL_PROCESS_ENV=1 NVRM_EVENT_WAIT=0` delante de su orden y
   compara ([EVENT-WAIT.md](EVENT-WAIT.md) explica cada número).
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
   - Ninguna línea: el proceso no lo pidió nunca (la tarjeta no es una RTX 50 por su id PCI, está puesto
     `NVMTL_NVENCVT=0`, o la biblioteca no está instalada con root como propietario), o todavía no ha arrancado ningún
     servicio de decodificación.
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

## Las vueltas atrás

Una por cambio; cada una devuelve el comportamiento de 2.9 solo de ese cambio. Están para saber si un cambio está
detrás de un problema, no para dejarlas puestas.

| Cambio | Vuelta atrás | Dónde |
|---|---|---|
| Valla de superficies | `nvrmfence=0` | argumento de arranque |
| Espera de page-off sin el cerrojo del acelerador | `nvaccel_pageoff_locked=1` | argumento de arranque |
| Superficies de dos planos en VRAM | `nvaccel_surf_planes=0` | argumento de arranque |
| Comprobaciones de las clases de Apple | `nvlayout=0` | argumento de arranque |
| Esperas del arranque | `-nvrmoldwaits` | argumento de arranque |
| Colas de trabajo del RM | `nvrmworkq=0` | argumento de arranque |
| Respuestas según el contexto | `nvrmatomic=0` | argumento de arranque |
| Hilo de la mitad inferior (bottom half) | `nvrmbhthread=0` | argumento de arranque |
| Temporizador RC | `nvrmrctimer=0` | argumento de arranque |
| Espera al cancelar un temporizador nano | `nvrmnanowait=0` | argumento de arranque |
| DMA a través de VT-d | `nvrmdma=0` (con un mapeador la tarjeta no arranca) | argumento de arranque |
| Espera por evento | `NVRM_EVENT_WAIT=0` (todo proceso sondea, WindowServer incluido) | `nvrm610.conf` |
| Recuperación de canales | `NVK_CHANNEL_RECOVER=0` | `nvrm610.conf` |
| Informe de error del trabajo descartado | `NVMTL_DROP_REPORT=0` | `nvrm610.conf` |
| Gracia de imágenes | `NVMTL_IMAGE_GRACE=0` | `nvrm610.conf` |
| Decodificación H.264 de RTX 50 en el servicio de decodificación | `NVMTL_NVENCVT=0` (la biblioteca de códecs apagada en todas partes) | `nvrm610.conf` |
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
