#!/usr/bin/env python3
"""The CGL adapter's dispatch table comes from the macOS SDK's public OpenGL headers (build/generate_cgl_dispatch.py):
gliDispatch.h's members, CGLMacro.h's pairs and the GL prototypes, joined by name and kept only where the signatures
agree. Its slots are offsetof() values the compiler takes from CGLContext.h, and nothing in the native OpenGL build
reads a binary to find a layout. This test feeds the generator's mapping synthetic headers, one rule at a time, and
holds zink/nvk_cgl.c and the build scripts to the header-derived layout.
usage: test_cgl_dispatch.py"""
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('generate_cgl_dispatch', ROOT / 'build/generate_cgl_dispatch.py')
gen = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT / 'build'))
spec.loader.exec_module(gen)
fails = 0


def check(what, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + what + ('' if ok else f'\n     {detail}'))
    fails += 0 if ok else 1


GLI = '''typedef struct __GLIFunctionDispatchRec
{
	void (*bind_buffer_ARB)(GLIContext ctx, GLenum target, GLuint buffer);
	void (*delete_object_ARB)(GLIContext ctx, GLhandleARB obj);
	void (*get_attached_objects_ARB)(GLIContext ctx, GLhandleARB containerObj, GLsizei maxCount, GLsizei *count, GLhandleARB *obj);
	void (*bind_vertex_array_ARB)(GLIContext ctx, GLuint array);
	void (*bind_vertex_array_EXT)(GLIContext ctx, GLuint id);
	void (*viewport)(GLIContext ctx, GLint x, GLint y, GLsizei width, GLsizei height);
	void *(*map_buffer_range)(GLIContext ctx, GLenum target, GLintptr offset, GLsizeiptr length, GLbitfield access);
	void (*vertex_pointer_EXT)(GLIContext ctx, GLint size, GLenum type, GLsizei stride, GLsizei count, const GLvoid *pointer);
	GLvoid *(*map_buffer)(GLIContext ctx, GLenum target, GLenum access);
	void (*swap_APPLE)(GLIContext ctx);
} GLIFunctionDispatch;
'''
MACRO = '''#define glDeleteProgram(program) \\
	(*(CGL_MACRO_CONTEXT)->disp.delete_object_ARB)(CGL_MACRO_CONTEXT_RENDERER, program)
#define glBindVertexArrayAPPLE(id) \\
	(*(CGL_MACRO_CONTEXT)->disp.bind_vertex_array_EXT)(CGL_MACRO_CONTEXT_RENDERER, id)
'''
PROTOS = '''extern void glBindBufferARB (GLenum target, GLuint buffer);
extern void glDeleteProgram (GLuint program);
extern void glGetAttachedShaders (GLuint program, GLsizei maxCount, GLsizei *count, GLuint *shaders);
extern void glGetAttachedObjectsARB (GLhandleARB containerObj, GLsizei maxCount, GLsizei *count, GLhandleARB *obj);
extern void glBindVertexArray (GLuint array);
extern void glBindVertexArrayAPPLE (GLuint array);
extern void glViewport (GLint x, GLint y, GLsizei width, GLsizei height);
extern void *glMapBufferRange (GLenum target, GLintptr offset, GLsizeiptr length, GLbitfield access);
extern void glVertexPointerEXT (GLint size, GLenum type, GLsizei stride, const GLvoid *pointer);
extern GLvoid *glMapBufferARB(GLenum target, GLenum access);
'''
CONTRACT = {'extensions': {'compatibility': {'GL_ARB_vertex_array_object': ['glBindVertexArray']}},
            'aliases': {'glGetAttachedShaders': 'glGetAttachedObjectsARB'}}

with tempfile.TemporaryDirectory(prefix='cgldispatch-') as t:
    h = Path(t)
    (h / 'gliDispatch.h').write_text(GLI)
    (h / 'CGLMacro.h').write_text(MACRO)
    (h / 'gl3.h').write_text(PROTOS)
    for n in ('gl3ext.h', 'gl.h', 'glext.h'):
        (h / n).write_text('#define GLAPI extern\n')
    members = gen.collect_members(h)
    prototypes = gen.collect_prototypes(h)
    pairs = gen.macro_pairs(h)
    slots, how, rejected = gen.build_map(prototypes, members, pairs, CONTRACT)
    owner = {n: m for m, ns in slots.items() for n in ns}

    check('gliDispatch.h: every function-pointer member is read, in order', [m for m, _, _ in members][:2] == ['bind_buffer_ARB', 'delete_object_ARB'] and len(members) == 10)
    check("CGLMacro.h's pairs are read", ('glDeleteProgram', 'delete_object_ARB') in pairs and len(pairs) == 2, pairs)
    check('a CGLMacro.h pair maps the core name to the ARB member (glDeleteProgram -> delete_object_ARB, GLuint and GLhandleARB pass alike)',
          owner.get('glDeleteProgram') == 'delete_object_ARB' and how.get('glDeleteProgram') == 'CGLMacro.h', how.get('glDeleteProgram'))
    check("a member's own spelling maps its name (bind_buffer_ARB -> glBindBufferARB)", owner.get('glBindBufferARB') == 'bind_buffer_ARB')
    check('a member without a suffix maps its plain name (viewport -> glViewport)', owner.get('glViewport') == 'viewport')
    check("the suffix-stripped name goes to the member whose suffix's extension provides it (glBindVertexArray -> bind_vertex_array_ARB)",
          owner.get('glBindVertexArray') == 'bind_vertex_array_ARB', owner.get('glBindVertexArray'))
    check("the APPLE VAO name stays on the member CGLMacro.h names (glBindVertexArrayAPPLE -> bind_vertex_array_EXT)",
          owner.get('glBindVertexArrayAPPLE') == 'bind_vertex_array_EXT')
    check('a GLbitfield and a GLenum pass alike, as do GLintptr and GLsizeiptr (glMapBufferRange -> map_buffer_range)',
          owner.get('glMapBufferRange') == 'map_buffer_range', rejected.get('glMapBufferRange'))
    check("glext.h's spelling 'extern GLvoid *glMapBufferARB(' is read, and the plain member takes the ARB name (map_buffer)",
          owner.get('glMapBufferARB') == 'map_buffer', (prototypes.get('glMapBufferARB'), rejected.get('glMapBufferARB')))
    check('a name whose prototype differs from the member is refused and recorded (glVertexPointerEXT: four parameters, the member five)',
          'glVertexPointerEXT' not in owner and 'glVertexPointerEXT' in rejected)
    check('an array of GLhandleARB is not an array of GLuint: the Khronos alias glGetAttachedShaders is refused for get_attached_objects_ARB',
          owner.get('glGetAttachedObjectsARB') == 'get_attached_objects_ARB' and 'glGetAttachedShaders' not in owner, (owner, rejected))
    check("the member the adapter handles itself is a member like any other here (swap_APPLE; nvk_cgl.c installs its own)",
          'swap_APPLE' in gen.OWN and 'swap_APPLE' in [m for m, _, _ in members])

    # controls: the rules above each make a difference
    sig = gen.signature
    check('control: GLuint * and GLhandleARB * differ after the reduction',
          sig('void', 'GLuint *a') != sig('void', 'GLhandleARB *a') and sig('void', 'GLuint a') == sig('void', 'GLhandleARB a'))
    check('control: GLshort and GLushort differ (the caller extends them by their sign)', sig('void', 'GLshort a') != sig('void', 'GLushort a'))
    s2, _, _ = gen.build_map(prototypes, members, pairs, {'extensions': {}, 'aliases': {}})
    check('control: with no extension of the registry providing it, glBindVertexArray reaches no member',
          not any('glBindVertexArray' in v for v in s2.values()), s2)
    (h / 'gliDispatch.h').write_text(GLI.replace('	void (*swap_APPLE)(GLIContext ctx);', '	GLuint count;'))
    try:
        gen.collect_members(h); refused = False
    except SystemExit:
        refused = True
    check('control: a member that is not a function pointer taking GLIContext stops the generator', refused)

src = (ROOT / 'zink/nvk_cgl.c').read_text()
check('nvk_cgl.c takes the GLI context prefix from CGLContext.h (offsetof(struct _CGLContextObject, priv))',
      re.search(r'#define\s+PREFIX_BYTES\s+offsetof\(struct _CGLContextObject,\s*priv\)', src) is not None)
check("nvk_cgl.c takes the swap slot from gliDispatch.h (offsetof(..., disp.swap_APPLE))",
      'disp.' in src and re.search(r'DISPATCH_SLOT\(swap_APPLE\)', src) is not None and '#include <OpenGL/CGLContext.h>' in src)
check("nvk_cgl.c's saved table is as long as the SDK's GLIFunctionDispatch", 'sizeof(GLIFunctionDispatch)' in src)
gen_text = (ROOT / 'build/generate_cgl_dispatch.py').read_text()
check('the generated entries are offsetof() values', 'offsetof(struct _CGLContextObject, disp.' in gen_text)
# hdz: no step of the native OpenGL build reads a binary for a layout (a debugger, a disassembler, the shared cache)
scripts = ['build/build_zink.sh', 'build/generate_cgl_dispatch.py', 'build/generate_gld_forward.py', 'build/prepare_zink.py',
           'build/opengl_api.py']
bad = [(s, w) for s in scripts for w in ('lldb', 'otool -tv', 'otool -t ', 'dyld_shared_cache', 'dsc_extractor', 'objdump -d', 'class-dump')
       if w in re.sub(r'#[^\n]*|"""[\s\S]*?"""', '', (ROOT / s).read_text())]
check('no step of the native OpenGL build runs a debugger or disassembler or reads the shared cache', not bad, bad)
check('the GL bundle generator reads export names only (nm -gU)', "'-gU'" in (ROOT / 'build/generate_gld_forward.py').read_text()
      or '-gU' in (ROOT / 'build/generate_gld_forward.py').read_text())
print(f'{fails} failed')
sys.exit(1 if fails else 0)
