/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * Metal AIR to NIR, for compute kernels and (since version 4 of the request) vertex and fragment functions, without
 * SPIR-V.
 *
 * AIR is LLVM bitcode with Apple's conventions: kernel arguments described by !air.kernel metadata (a vertex or fragment
 * function's by !air.vertex or !air.fragment, with its outputs), builtins named
 * air.<function>.<type>, address spaces 0 thread, 1 device, 2 constant, 3 threadgroup. This file reads it with LLVM's C
 * API (from the LLVM library the plugin already ships, opened at run time, so NVK links no LLVM) and builds NIR with
 * nir_builder, in NVK's own copy of the compiler, for the plugin to hand to vkCreateComputePipelines (or
 * vkCreateGraphicsPipelines) through VkPipelineShaderStageNirCreateInfoMESA. The interface, and the descriptor and push-constant contract the result
 * reads, are in nvmtl_air_nir_abi.h.
 *
 * The design follows the one Stela (an AIR-to-NIR compiler for NVK, GPL) showed works: walk LLVM's CFG into
 * unstructured NIR and let nir_lower_goto_ifs structure it, phis through local variables that nir_lower_vars_to_ssa
 * folds back, pointers as plain addresses. None of Stela's code is used.
 *
 * Memory model:
 *   device and constant pointers   64-bit GPU addresses: a buffer argument's from the address table, then
 *                                  load_global/store_global/global_atomic
 *   constant tables (globals)      offsets into the shader's constant data, load_constant
 *   threadgroup pointers           32-bit offsets into shared memory: static globals first, then the
 *                                  [[threadgroup(n)]] arguments at the lengths the request gives
 *   thread pointers                32-bit offsets into scratch memory, one fixed slot per alloca LLVM left
 *
 * Anything outside what this translates is refused with a message, never guessed: the caller keeps using the
 * SPIR-V translator for it.
 */
#include "nvk_device.h"
#include "nvk_physical_device.h"
#include "nvk_shader.h"

#include "nak.h"
#include "nir.h"
#include "nir_builder.h"
#include "nir_builtin_builder.h"
#include "nir_serialize.h"
#include "util/blob.h"
#include "util/half_float.h"
#include "util/hash_table.h"
#include "util/set.h"
#include "util/ralloc.h"
#include "util/simple_mtx.h"
#include "util/u_dynarray.h"
#include "vk_pipeline.h"

#include "nvmtl_air_nir_abi.h"

#include <dlfcn.h>
#include <math.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#ifndef M_LOG2E
#define M_LOG2E 1.4426950408889634074
#endif
#ifndef M_LN2
#define M_LN2 0.69314718055994530942
#endif

/* ---------------------------------------------------------------------------------------------------------------- */
/* LLVM's C API, found at run time                                                                                    */

typedef struct air_llvm_context *LLVMContextRef;
typedef struct air_llvm_module *LLVMModuleRef;
typedef struct air_llvm_value *LLVMValueRef;
typedef struct air_llvm_type *LLVMTypeRef;
typedef struct air_llvm_block *LLVMBasicBlockRef;
typedef struct air_llvm_buffer *LLVMMemoryBufferRef;
typedef struct air_llvm_target_data *LLVMTargetDataRef;
typedef struct air_llvm_error *LLVMErrorRef;
typedef struct air_llvm_pass_options *LLVMPassBuilderOptionsRef;
typedef struct air_llvm_diagnostic *LLVMDiagnosticInfoRef;
typedef struct air_llvm_attribute *LLVMAttributeRef;
typedef struct air_llvm_use *LLVMUseRef;
typedef void (*LLVMDiagnosticHandler)(LLVMDiagnosticInfoRef, void *);

/* llvm-c/Core.h: LLVMOpcode and LLVMTypeKind keep their values across LLVM releases */
enum {
   OP_RET = 1, OP_BR = 2, OP_SWITCH = 3, OP_UNREACHABLE = 7, OP_ADD = 8, OP_FADD = 9, OP_SUB = 10, OP_FSUB = 11,
   OP_MUL = 12, OP_FMUL = 13, OP_UDIV = 14, OP_SDIV = 15, OP_FDIV = 16, OP_UREM = 17, OP_SREM = 18, OP_FREM = 19,
   OP_SHL = 20, OP_LSHR = 21, OP_ASHR = 22, OP_AND = 23, OP_OR = 24, OP_XOR = 25, OP_ALLOCA = 26, OP_LOAD = 27,
   OP_STORE = 28, OP_GEP = 29, OP_TRUNC = 30, OP_ZEXT = 31, OP_SEXT = 32, OP_FPTOUI = 33, OP_FPTOSI = 34,
   OP_UITOFP = 35, OP_SITOFP = 36, OP_FPTRUNC = 37, OP_FPEXT = 38, OP_PTRTOINT = 39, OP_INTTOPTR = 40,
   OP_BITCAST = 41, OP_ICMP = 42, OP_FCMP = 43, OP_PHI = 44, OP_CALL = 45, OP_SELECT = 46, OP_EXTRACTELEMENT = 50,
   OP_INSERTELEMENT = 51, OP_SHUFFLEVECTOR = 52, OP_EXTRACTVALUE = 53, OP_INSERTVALUE = 54, OP_ADDRSPACECAST = 60,
   OP_FNEG = 66, OP_FREEZE = 68,
};
enum {
   TY_VOID = 0, TY_HALF = 1, TY_FLOAT = 2, TY_DOUBLE = 3, TY_INTEGER = 8, TY_STRUCT = 10, TY_ARRAY = 11,
   TY_POINTER = 12, TY_VECTOR = 13,
};
enum { ICMP_EQ = 32, ICMP_NE, ICMP_UGT, ICMP_UGE, ICMP_ULT, ICMP_ULE, ICMP_SGT, ICMP_SGE, ICMP_SLT, ICMP_SLE };
enum {
   FCMP_FALSE, FCMP_OEQ, FCMP_OGT, FCMP_OGE, FCMP_OLT, FCMP_OLE, FCMP_ONE, FCMP_ORD, FCMP_UNO, FCMP_UEQ, FCMP_UGT,
   FCMP_UGE, FCMP_ULT, FCMP_ULE, FCMP_UNE, FCMP_TRUE,
};

#define AIR_LLVM_FUNCTIONS(X)                                                                                   \
   X(LLVMContextRef, LLVMContextCreate, (void))                                                                  \
   X(void, LLVMContextDispose, (LLVMContextRef))                                                                 \
   X(void, LLVMContextSetDiagnosticHandler, (LLVMContextRef, LLVMDiagnosticHandler, void *))                     \
   X(char *, LLVMGetDiagInfoDescription, (LLVMDiagnosticInfoRef))                                                \
   X(int, LLVMGetDiagInfoSeverity, (LLVMDiagnosticInfoRef))                                                      \
   X(void, LLVMDisposeMessage, (char *))                                                                         \
   X(LLVMMemoryBufferRef, LLVMCreateMemoryBufferWithMemoryRangeCopy, (const char *, size_t, const char *))       \
   X(void, LLVMDisposeMemoryBuffer, (LLVMMemoryBufferRef))                                                       \
   X(int, LLVMParseBitcodeInContext2, (LLVMContextRef, LLVMMemoryBufferRef, LLVMModuleRef *))                    \
   X(int, LLVMParseIRInContext, (LLVMContextRef, LLVMMemoryBufferRef, LLVMModuleRef *, char **))                 \
   X(void, LLVMDisposeModule, (LLVMModuleRef))                                                                   \
   X(LLVMErrorRef, LLVMRunPasses, (LLVMModuleRef, const char *, void *, LLVMPassBuilderOptionsRef))              \
   X(LLVMPassBuilderOptionsRef, LLVMCreatePassBuilderOptions, (void))                                            \
   X(void, LLVMDisposePassBuilderOptions, (LLVMPassBuilderOptionsRef))                                           \
   X(char *, LLVMGetErrorMessage, (LLVMErrorRef))                                                                \
   X(void, LLVMDisposeErrorMessage, (char *))                                                                    \
   X(unsigned, LLVMGetNamedMetadataNumOperands, (LLVMModuleRef, const char *))                                   \
   X(void, LLVMGetNamedMetadataOperands, (LLVMModuleRef, const char *, LLVMValueRef *))                          \
   X(unsigned, LLVMGetMDNodeNumOperands, (LLVMValueRef))                                                         \
   X(void, LLVMGetMDNodeOperands, (LLVMValueRef, LLVMValueRef *))                                                \
   X(const char *, LLVMGetMDString, (LLVMValueRef, unsigned *))                                                  \
   X(LLVMTargetDataRef, LLVMGetModuleDataLayout, (LLVMModuleRef))                                                \
   X(unsigned long long, LLVMABISizeOfType, (LLVMTargetDataRef, LLVMTypeRef))                                    \
   X(unsigned long long, LLVMStoreSizeOfType, (LLVMTargetDataRef, LLVMTypeRef))                                  \
   X(unsigned, LLVMABIAlignmentOfType, (LLVMTargetDataRef, LLVMTypeRef))                                         \
   X(unsigned long long, LLVMOffsetOfElement, (LLVMTargetDataRef, LLVMTypeRef, unsigned))                        \
   X(unsigned, LLVMCountParams, (LLVMValueRef))                                                                  \
   X(LLVMValueRef, LLVMGetParam, (LLVMValueRef, unsigned))                                                       \
   X(LLVMBasicBlockRef, LLVMGetEntryBasicBlock, (LLVMValueRef))                                                  \
   X(LLVMValueRef, LLVMGetFirstInstruction, (LLVMBasicBlockRef))                                                 \
   X(LLVMValueRef, LLVMGetNextInstruction, (LLVMValueRef))                                                       \
   X(LLVMValueRef, LLVMGetBasicBlockTerminator, (LLVMBasicBlockRef))                                             \
   X(int, LLVMGetInstructionOpcode, (LLVMValueRef))                                                              \
   X(int, LLVMGetNumOperands, (LLVMValueRef))                                                                    \
   X(LLVMValueRef, LLVMGetOperand, (LLVMValueRef, unsigned))                                                     \
   X(LLVMTypeRef, LLVMTypeOf, (LLVMValueRef))                                                                    \
   X(int, LLVMGetTypeKind, (LLVMTypeRef))                                                                        \
   X(unsigned, LLVMGetIntTypeWidth, (LLVMTypeRef))                                                               \
   X(unsigned, LLVMGetVectorSize, (LLVMTypeRef))                                                                 \
   X(LLVMTypeRef, LLVMGetElementType, (LLVMTypeRef))                                                             \
   X(unsigned, LLVMCountStructElementTypes, (LLVMTypeRef))                                                       \
   X(LLVMTypeRef, LLVMStructGetTypeAtIndex, (LLVMTypeRef, unsigned))                                             \
   X(unsigned, LLVMGetPointerAddressSpace, (LLVMTypeRef))                                                        \
   X(LLVMValueRef, LLVMIsAConstantInt, (LLVMValueRef))                                                           \
   X(LLVMValueRef, LLVMIsAConstantFP, (LLVMValueRef))                                                            \
   X(LLVMValueRef, LLVMIsAConstantExpr, (LLVMValueRef))                                                          \
   X(LLVMValueRef, LLVMIsAConstantAggregateZero, (LLVMValueRef))                                                 \
   X(LLVMValueRef, LLVMIsAConstantDataSequential, (LLVMValueRef))                                                \
   X(LLVMValueRef, LLVMIsAConstantVector, (LLVMValueRef))                                                        \
   X(LLVMValueRef, LLVMIsAConstantArray, (LLVMValueRef))                                                         \
   X(LLVMValueRef, LLVMIsAConstantStruct, (LLVMValueRef))                                                        \
   X(LLVMValueRef, LLVMIsAConstantPointerNull, (LLVMValueRef))                                                   \
   X(LLVMValueRef, LLVMIsAUndefValue, (LLVMValueRef))                                                            \
   X(LLVMValueRef, LLVMIsAGlobalVariable, (LLVMValueRef))                                                        \
   X(LLVMValueRef, LLVMIsAFunction, (LLVMValueRef))                                                              \
   X(LLVMValueRef, LLVMIsAMDString, (LLVMValueRef))                                                              \
   X(LLVMValueRef, LLVMIsAMDNode, (LLVMValueRef))                                                                \
   X(unsigned long long, LLVMConstIntGetZExtValue, (LLVMValueRef))                                               \
   X(long long, LLVMConstIntGetSExtValue, (LLVMValueRef))                                                        \
   X(double, LLVMConstRealGetDouble, (LLVMValueRef, int *))                                                      \
   X(LLVMValueRef, LLVMGetAggregateElement, (LLVMValueRef, unsigned))                                            \
   X(int, LLVMGetConstOpcode, (LLVMValueRef))                                                                    \
   X(LLVMValueRef, LLVMGetInitializer, (LLVMValueRef))                                                           \
   X(LLVMTypeRef, LLVMGlobalGetValueType, (LLVMValueRef))                                                        \
   X(LLVMTypeRef, LLVMGetReturnType, (LLVMTypeRef))                                                              \
   X(unsigned, LLVMGetAlignment, (LLVMValueRef))                                                                 \
   X(int, LLVMIsDeclaration, (LLVMValueRef))                                                                     \
   X(const char *, LLVMGetValueName2, (LLVMValueRef, size_t *))                                                  \
   X(int, LLVMGetICmpPredicate, (LLVMValueRef))                                                                  \
   X(int, LLVMGetFCmpPredicate, (LLVMValueRef))                                                                  \
   X(LLVMValueRef, LLVMGetCalledValue, (LLVMValueRef))                                                           \
   X(unsigned, LLVMGetNumArgOperands, (LLVMValueRef))                                                            \
   X(unsigned, LLVMCountIncoming, (LLVMValueRef))                                                                \
   X(LLVMValueRef, LLVMGetIncomingValue, (LLVMValueRef, unsigned))                                               \
   X(LLVMBasicBlockRef, LLVMGetIncomingBlock, (LLVMValueRef, unsigned))                                          \
   X(unsigned, LLVMGetNumSuccessors, (LLVMValueRef))                                                             \
   X(LLVMBasicBlockRef, LLVMGetSuccessor, (LLVMValueRef, unsigned))                                              \
   X(int, LLVMIsConditional, (LLVMValueRef))                                                                     \
   X(LLVMValueRef, LLVMGetCondition, (LLVMValueRef))                                                             \
   X(LLVMTypeRef, LLVMGetGEPSourceElementType, (LLVMValueRef))                                                   \
   X(unsigned, LLVMGetNumMaskElements, (LLVMValueRef))                                                           \
   X(int, LLVMGetMaskValue, (LLVMValueRef, unsigned))                                                            \
   X(LLVMTypeRef, LLVMGetAllocatedType, (LLVMValueRef))                                                          \
   X(LLVMValueRef, LLVMGetFirstFunction, (LLVMModuleRef))                                                        \
   X(LLVMValueRef, LLVMGetNextFunction, (LLVMValueRef))                                                          \
   X(unsigned, LLVMGetEnumAttributeKindForName, (const char *, size_t))                                          \
   X(LLVMAttributeRef, LLVMGetEnumAttributeAtIndex, (LLVMValueRef, unsigned, unsigned))                          \
   X(LLVMAttributeRef, LLVMCreateEnumAttribute, (LLVMContextRef, unsigned, uint64_t))                            \
   X(void, LLVMAddAttributeAtIndex, (LLVMValueRef, unsigned, LLVMAttributeRef))                                  \
   X(void, LLVMRemoveEnumAttributeAtIndex, (LLVMValueRef, unsigned, unsigned))                                   \
   X(uint64_t, LLVMGetArrayLength2, (LLVMTypeRef))                                                               \
   X(unsigned, LLVMGetNumIndices, (LLVMValueRef))                                                                \
   X(const unsigned *, LLVMGetIndices, (LLVMValueRef))                                                           \
   X(int, LLVMIsGlobalConstant, (LLVMValueRef))                                                                  \
   X(int, LLVMIsExternallyInitialized, (LLVMValueRef))                                                           \
   X(LLVMValueRef, LLVMGetFirstGlobal, (LLVMModuleRef))                                                          \
   X(LLVMValueRef, LLVMGetNextGlobal, (LLVMValueRef))                                                            \
   X(const char *, LLVMGetSection, (LLVMValueRef))                                                               \
   X(void, LLVMSetInitializer, (LLVMValueRef, LLVMValueRef))                                                     \
   X(void, LLVMSetGlobalConstant, (LLVMValueRef, int))                                                           \
   X(void, LLVMSetExternallyInitialized, (LLVMValueRef, int))                                                    \
   X(LLVMValueRef, LLVMConstInt, (LLVMTypeRef, unsigned long long, int))                                         \
   X(LLVMValueRef, LLVMConstReal, (LLVMTypeRef, double))                                                         \
   X(LLVMValueRef, LLVMConstNull, (LLVMTypeRef))                                                                 \
   X(LLVMValueRef, LLVMConstVector, (LLVMValueRef *, unsigned))                                                  \
   X(LLVMValueRef, LLVMConstArray2, (LLVMTypeRef, LLVMValueRef *, uint64_t))                                     \
   X(LLVMValueRef, LLVMConstNamedStruct, (LLVMTypeRef, LLVMValueRef *, unsigned))                                \
   X(LLVMValueRef, LLVMConstStructInContext, (LLVMContextRef, LLVMValueRef *, unsigned, int))                    \
   X(int, LLVMIsPackedStruct, (LLVMTypeRef))                                                                     \
   X(const char *, LLVMGetStructName, (LLVMTypeRef))                                                             \
   X(LLVMTypeRef, LLVMInt1TypeInContext, (LLVMContextRef))                                                       \
   X(LLVMValueRef, LLVMGetNamedFunction, (LLVMModuleRef, const char *))                                          \
   X(LLVMUseRef, LLVMGetFirstUse, (LLVMValueRef))                                                                \
   X(LLVMUseRef, LLVMGetNextUse, (LLVMUseRef))                                                                   \
   X(LLVMValueRef, LLVMGetUser, (LLVMUseRef))                                                                    \
   X(void, LLVMReplaceAllUsesWith, (LLVMValueRef, LLVMValueRef))                                                 \
   X(void, LLVMInstructionEraseFromParent, (LLVMValueRef))                                                       \
   X(LLVMBasicBlockRef, LLVMGetInstructionParent, (LLVMValueRef))                                                \
   X(LLVMBasicBlockRef, LLVMGetFirstBasicBlock, (LLVMValueRef))                                                  \
   X(LLVMBasicBlockRef, LLVMGetNextBasicBlock, (LLVMBasicBlockRef))                                              \
   X(char *, LLVMPrintModuleToString, (LLVMModuleRef))

static struct {
#define X(ret, name, args) ret(*name) args;
   AIR_LLVM_FUNCTIONS(X)
#undef X
   void *handle;
   char path[1024];
   char missing[96];
} L;
static simple_mtx_t air_llvm_lock = SIMPLE_MTX_INITIALIZER;

/* Opens the library once per process; a second request naming another library is refused rather than mixing two
 * LLVMs' objects. */
static bool
air_llvm_open(const char *path, char *why, size_t whylen)
{
   simple_mtx_lock(&air_llvm_lock);
   bool ok = false;
   if (L.handle) {
      ok = !strcmp(L.path, path);
      if (!ok)
         snprintf(why, whylen, "LLVM is already open from %s, not %s", L.path, path);
      goto out;
   }
   void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
   if (!h) {
      snprintf(why, whylen, "cannot open %s: %s", path, dlerror());
      goto out;
   }
#define X(ret, name, args)                                                                                      \
   L.name = (ret(*) args)dlsym(h, #name);                                                                       \
   if (!L.name) {                                                                                               \
      snprintf(why, whylen, "%s has no %s", path, #name);                                                       \
      dlclose(h);                                                                                               \
      goto out;                                                                                                 \
   }
   AIR_LLVM_FUNCTIONS(X)
#undef X
   L.handle = h;
   snprintf(L.path, sizeof L.path, "%s", path);
   ok = true;
out:
   simple_mtx_unlock(&air_llvm_lock);
   return ok;
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Translation state                                                                                                  */

enum air_kind {
   AIR_VALUE,     /* not a pointer */
   AIR_GLOBAL,    /* 64-bit GPU address */
   AIR_CONSTANT,  /* 32-bit offset into the shader's constant data */
   AIR_SHARED,    /* 32-bit offset into threadgroup memory */
   AIR_SCRATCH,   /* 32-bit offset into thread memory */
   AIR_AGGREGATE, /* a structure or array held as a value: its elements, no def */
   AIR_TEXTURE,   /* a [[texture(n)]] argument: res is its struct air_texture, no def */
   AIR_SAMPLER,   /* a sampler: res is its struct air_sampler, no def */
   AIR_TEXARRAY,  /* a pointer into an array<texture, N> argument: res is its struct air_texarray, def the element */
};

struct air_val {
   nir_def *def;
   uint8_t kind;
   uint32_t align;    /* known alignment of a pointer, in bytes */
   unsigned num_elems;
   struct air_val **elems;   /* AIR_AGGREGATE */
   void *res;                /* AIR_TEXTURE, AIR_SAMPLER, AIR_TEXARRAY */
};

#define AIR_MAX_ELEMS 256    /* of one structure or array held as a value */
#define AIR_MAX_DEPTH 8      /* aggregates nested in a phi */

struct air_block {
   LLVMBasicBlockRef llvm;
   nir_block *nir;
   nir_intrinsic_instr *end;   /* a nop before the jump: phi stores go after it */
   struct list_head link;
};

struct air_phi {
   LLVMValueRef phi;
   nir_variable *var;
   uint8_t kind;
   uint8_t depth;                  /* an element of an aggregate phi: the indices down to it */
   uint16_t path[AIR_MAX_DEPTH];
};

struct air_tr {
   void *mem;
   jmp_buf fail;
   char *message;
   size_t message_len;
   int status;

   const struct nvmtl_air_nir_request *req;
   LLVMModuleRef module;
   LLVMTargetDataRef layout;
   LLVMValueRef function;

   nir_shader *nir;
   nir_function_impl *impl;
   nir_builder b;     /* the instruction being translated */
   bool math_fast;    /* the air.* math call being translated is a fast_ one (air_float_function) */
   nir_builder top;   /* the start of the shader: arguments, builtins, constants */

   struct hash_table *values;   /* LLVMValueRef -> struct air_val */
   struct hash_table *blocks;   /* LLVMBasicBlockRef -> struct air_block */
   struct hash_table *globals;  /* LLVMValueRef -> struct air_val (the global's address) */
   struct util_dynarray phis;   /* struct air_phi */
   struct util_dynarray constant_data;

   nir_variable *push;
   nir_def *push_word[12];
   nir_def *table;              /* the address table's descriptor */
   nir_variable_mode table_mode;
   nir_address_format table_format;
   const struct glsl_type *table_type;

   uint32_t shared_size, scratch_size, buffer_mask, instructions;
   uint32_t global_align;       /* the largest alignment a device or constant buffer access promises */
   uint32_t texture_mask[4], storage_mask[4], texbuf_mask[4], stexbuf_mask[4], sampler_mask;
   uint32_t sampler_args;       /* sampler slots [[sampler(n)]] arguments declare, used or not */
   struct util_dynarray statics; /* struct air_sampler *: the constexpr samplers, in their slot order */
   nir_def *num_workgroups;
   nir_shader *out;
   char llvm_error[160];

   /* a vertex or fragment function (since version 4) */
   unsigned stage;              /* NVMTL_AIR_NIR_STAGE_* */
   unsigned set;                /* the descriptor set of its resources: 0, NVMTL_AIR_NIR_FRAGMENT_SET for a fragment */
   LLVMValueRef outputs_node;   /* the entry's outputs: the members of what it returns */
   struct air_output *outputs;
   unsigned num_outputs;
   uint32_t vertex_inputs, varyings, color_outputs, fragment_flags;
   uint32_t color_inputs;       /* [[color(n)]] inputs declared (since version 6, NVMTL_AIR_NIR_COLOR_INPUTS) */
   bool heap_textures;          /* an array<texture, N> element is read through the bindless heap (version 6) */
   uint8_t vertex_input_kind[32];
   unsigned next_varying;       /* the translator's own Locations: the next one in order */
   nir_variable *sysval[8];     /* the system values read, one variable each */

   /* the reflection (since version 5, NVMTL_AIR_NIR_REFLECT) */
   uint8_t *rf_param;           /* the entry's parameters before the passes: AIR_RF_* */
   unsigned rf_params;
   bool rf_rewrite;             /* the translator's specialization rewrites function-constant gates (values given) */
   bool rf_computed_gate;       /* a gate's value came from a global the static initializer computes ... */
   bool rf_init_inexact;        /* ... where the translator's straight-line text evaluation of it may differ (it takes
                                 * is_function_constant_defined as false, and every block in text order) */
   char *rf_json;
   const char *rf_why;
};

/* One member of a vertex or fragment function's return value: where it goes. */
struct air_output {
   bool dropped;                   /* it exists only under a function constant that is false: nothing is written */
   nir_variable *var;
   unsigned array;                 /* clip distances: the length of the compact array, else 0 */
   enum glsl_base_type base;       /* of the variable's components (32-bit) */
   unsigned components;
};

static void
air_llvm_diagnostic(LLVMDiagnosticInfoRef info, void *data)
{
   struct air_tr *t = data;
   if (L.LLVMGetDiagInfoSeverity(info) != 0 /* LLVMDSError */ || t->llvm_error[0])
      return;
   char *d = L.LLVMGetDiagInfoDescription(info);
   if (d) {
      snprintf(t->llvm_error, sizeof t->llvm_error, "%s", d);
      L.LLVMDisposeMessage(d);
   }
}

static void PRINTFLIKE(3, 4) NORETURN
air_refuse(struct air_tr *t, int status, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   vsnprintf(t->message, t->message_len, fmt, ap);
   va_end(ap);
   t->status = status;
   longjmp(t->fail, 1);
}
#define unsupported(t, ...) air_refuse(t, NVMTL_AIR_NIR_UNSUPPORTED, __VA_ARGS__)

_Static_assert(offsetof(struct nvmtl_air_nir_request, fc_count) == NVMTL_AIR_NIR_REQUEST_V1_SIZE,
               "version 1 of the request ends at fc_count");
_Static_assert(offsetof(struct nvmtl_air_nir_request, varying_count) == NVMTL_AIR_NIR_REQUEST_V3_SIZE,
               "version 3 of the request ends at varying_count");
_Static_assert(offsetof(struct nvmtl_air_nir_result, stage) == NVMTL_AIR_NIR_RESULT_V3_SIZE,
               "version 3 of the result ends at stage");
_Static_assert(offsetof(struct nvmtl_air_nir_request, sampler_state_count) == NVMTL_AIR_NIR_REQUEST_V4_SIZE,
               "version 4 of the request ends at sampler_state_count");
_Static_assert(offsetof(struct nvmtl_air_nir_result, reflection) == NVMTL_AIR_NIR_RESULT_V4_SIZE,
               "version 4 of the result ends at reflection");
_Static_assert(offsetof(struct nvmtl_air_nir_result, color_inputs) == NVMTL_AIR_NIR_RESULT_V5_SIZE,
               "version 5 of the result ends at color_inputs");
_Static_assert(offsetof(struct nvmtl_air_nir_result, texture_mask) == NVMTL_AIR_NIR_RESULT_V1_SIZE,
               "version 1 of the result ends at its message");
_Static_assert(offsetof(struct nvmtl_air_nir_result, global_align) == NVMTL_AIR_NIR_RESULT_V2_SIZE,
               "version 2 of the result ends at its constexpr samplers");

static const char *
air_name(LLVMValueRef v, size_t *len)
{
   size_t n = 0;
   const char *s = L.LLVMGetValueName2(v, &n);
   if (len)
      *len = n;
   return s ? s : "";
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Types                                                                                                              */

struct air_type {
   unsigned bits;          /* of one component in NIR; 1 for a bool */
   unsigned width;         /* of the LLVM integer: an i24 is 24 bits held zero-extended in 32 */
   unsigned components;
   bool is_float;
   bool is_pointer;
   unsigned address_space;
};

static struct air_type
air_type_of(struct air_tr *t, LLVMTypeRef ty)
{
   struct air_type r = { .components = 1 };
   if (L.LLVMGetTypeKind(ty) == TY_VECTOR) {
      r.components = L.LLVMGetVectorSize(ty);
      ty = L.LLVMGetElementType(ty);
      if (r.components < 1 || (r.components > 4 && r.components != 8 && r.components != 16))
         unsupported(t, "a vector of %u components", r.components);
   }
   switch (L.LLVMGetTypeKind(ty)) {
   case TY_INTEGER:
      /* SROA leaves odd widths (i24, i48) where it splits byte copies */
      r.width = L.LLVMGetIntTypeWidth(ty);
      if (r.width == 1)
         r.bits = 1;
      else if (r.width <= 64)
         r.bits = MAX2(8, util_next_power_of_two(r.width));
      else
         unsupported(t, "an i%u value", r.width);
      break;
   case TY_HALF: r.bits = 16; r.is_float = true; break;
   case TY_FLOAT: r.bits = 32; r.is_float = true; break;
   case TY_POINTER:
      r.is_pointer = true;
      r.address_space = L.LLVMGetPointerAddressSpace(ty);
      r.bits = (r.address_space == 1 || r.address_space == 2) ? 64 : 32;
      if (r.address_space > 3)
         unsupported(t, "a pointer in address space %u", r.address_space);
      break;
   case TY_DOUBLE: unsupported(t, "a double");
   default: unsupported(t, "a value of LLVM type kind %d", L.LLVMGetTypeKind(ty));
   }
   if (r.is_pointer && r.components != 1)
      unsupported(t, "a vector of pointers");
   if (!r.width)
      r.width = r.bits;
   return r;
}

static enum air_kind
air_kind_of_space(unsigned space)
{
   switch (space) {
   case 1: case 2: return AIR_GLOBAL;
   case 3: return AIR_SHARED;
   default: return AIR_SCRATCH;
   }
}

static const struct glsl_type *
air_glsl_type(const struct air_type *ty)
{
   enum glsl_base_type base;
   switch (ty->bits) {
   case 1: base = GLSL_TYPE_BOOL; break;
   case 8: base = GLSL_TYPE_UINT8; break;
   case 16: base = GLSL_TYPE_UINT16; break;
   case 64: base = GLSL_TYPE_UINT64; break;
   default: base = GLSL_TYPE_UINT; break;
   }
   return glsl_vector_type(base, ty->components);
}

static uint64_t
air_size(struct air_tr *t, LLVMTypeRef ty)
{
   return L.LLVMABISizeOfType(t->layout, ty);
}

/* An odd-width integer is kept zero-extended: clear what an operation carried above its width... */
static nir_def *
air_canon(nir_builder *b, nir_def *v, unsigned width)
{
   return width < v->bit_size && v->bit_size > 1 ? nir_iand_imm(b, v, BITFIELD64_MASK(width)) : v;
}

/* ...and sign-extend it within its container where the sign matters. */
static nir_def *
air_signed(nir_builder *b, nir_def *v, unsigned width)
{
   if (width >= v->bit_size || v->bit_size == 1)
      return v;
   unsigned shift = v->bit_size - width;
   return nir_ishr_imm(b, nir_ishl_imm(b, v, shift), shift);
}

static unsigned
air_width_of(struct air_tr *t, LLVMValueRef v)
{
   return air_type_of(t, L.LLVMTypeOf(v)).width;
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Values                                                                                                             */

static struct air_val *air_get(struct air_tr *t, LLVMValueRef v);

static struct air_val *
air_make(struct air_tr *t, nir_def *def, enum air_kind kind, uint32_t align)
{
   struct air_val *r = rzalloc(t->mem, struct air_val);
   r->def = def;
   r->kind = kind;
   r->align = align ? align : 1;
   return r;
}

static struct air_val *
air_set_val(struct air_tr *t, LLVMValueRef v, struct air_val *r)
{
   _mesa_hash_table_insert(t->values, v, r);
   return r;
}

static struct air_val *
air_set(struct air_tr *t, LLVMValueRef v, nir_def *def, enum air_kind kind, uint32_t align)
{
   return air_set_val(t, v, air_make(t, def, kind, align));
}

/* Structures and arrays held as values (insertvalue and extractvalue, loads, stores, phis and selects of them): a tree
 * of their elements, each a value of its own. */
static bool
air_is_aggregate(LLVMTypeRef ty)
{
   int k = L.LLVMGetTypeKind(ty);
   return k == TY_STRUCT || k == TY_ARRAY;
}

static unsigned
air_num_elems(struct air_tr *t, LLVMTypeRef ty)
{
   uint64_t n = L.LLVMGetTypeKind(ty) == TY_STRUCT ? L.LLVMCountStructElementTypes(ty) : L.LLVMGetArrayLength2(ty);
   if (n > AIR_MAX_ELEMS)
      unsupported(t, "an aggregate value of %llu elements", (unsigned long long)n);
   return (unsigned)n;
}

static LLVMTypeRef
air_elem_type(LLVMTypeRef ty, unsigned i)
{
   return L.LLVMGetTypeKind(ty) == TY_STRUCT ? L.LLVMStructGetTypeAtIndex(ty, i) : L.LLVMGetElementType(ty);
}

static uint64_t
air_elem_offset(struct air_tr *t, LLVMTypeRef ty, unsigned i)
{
   if (L.LLVMGetTypeKind(ty) == TY_STRUCT)
      return L.LLVMOffsetOfElement(t->layout, ty, i);
   return (uint64_t)i * L.LLVMABISizeOfType(t->layout, L.LLVMGetElementType(ty));
}

static struct air_val *
air_aggregate(struct air_tr *t, unsigned n)
{
   struct air_val *r = air_make(t, NULL, AIR_AGGREGATE, 1);
   r->num_elems = n;
   r->elems = rzalloc_array(t->mem, struct air_val *, n ? n : 1);
   return r;
}

/* The value's NIR def, refusing a texture or sampler handle (and an aggregate) used as a plain value: they have no def,
 * and Apple's compiler writes is_null_texture of a handle as a ptrtoint and a compare (a SceneKit fragment did). */
static nir_def *
air_vdef(struct air_tr *t, const struct air_val *x)
{
   if (!x->def)
      unsupported(t, "a %s used as a value (a null-texture test, a select or a phi of handles, ...)",
                  x->kind == AIR_TEXTURE ? "texture handle" : x->kind == AIR_SAMPLER ? "sampler handle" : "value without one");
   return x->def;
}

static nir_def *
air_def(struct air_tr *t, LLVMValueRef v)
{
   return air_vdef(t, air_get(t, v));
}

static uint32_t
air_gcd_align(uint32_t align, uint64_t offset)
{
   if (!offset)
      return align;
   uint32_t low = (uint32_t)(offset & (~offset + 1));
   if (!low || low > align)
      return align;
   return low;
}

/* A scalar constant of a type, at the top of the shader. */
static nir_def *
air_scalar_constant(struct air_tr *t, LLVMValueRef c, const struct air_type *ty)
{
   nir_builder *b = &t->top;
   if (L.LLVMIsAUndefValue(c))
      return nir_undef(b, 1, ty->bits);
   if (L.LLVMIsAConstantPointerNull(c) || L.LLVMIsAConstantAggregateZero(c))
      return ty->bits == 1 ? nir_imm_false(b) : nir_imm_intN_t(b, 0, ty->bits);
   if (L.LLVMIsAConstantInt(c)) {
      uint64_t u = L.LLVMConstIntGetZExtValue(c);
      return ty->bits == 1 ? nir_imm_bool(b, u & 1) : nir_imm_intN_t(b, u, ty->bits);
   }
   if (L.LLVMIsAConstantFP(c)) {
      int loses = 0;
      double d = L.LLVMConstRealGetDouble(c, &loses);
      if (ty->bits == 16)
         return nir_imm_intN_t(b, _mesa_float_to_half((float)d), 16);
      return nir_imm_float(b, (float)d);
   }
   unsupported(t, "a constant of an unknown kind");
}

static struct air_val *air_constant_expr(struct air_tr *t, LLVMValueRef c);
static struct air_val *air_global(struct air_tr *t, LLVMValueRef g);

static struct air_val *
air_constant(struct air_tr *t, LLVMValueRef c)
{
   LLVMTypeRef lty = L.LLVMTypeOf(c);
   struct air_type ty = air_type_of(t, lty);
   if (L.LLVMIsAGlobalVariable(c))
      return air_global(t, c);
   if (L.LLVMIsAConstantExpr(c))
      return air_constant_expr(t, c);
   if (ty.is_pointer) {
      if (L.LLVMIsAConstantPointerNull(c) || L.LLVMIsAUndefValue(c))
         return air_set(t, c, nir_imm_intN_t(&t->top, 0, ty.bits), air_kind_of_space(ty.address_space), 16);
      unsupported(t, "a constant pointer of an unknown kind");
   }
   if (ty.components == 1)
      return air_set(t, c, air_scalar_constant(t, c, &ty), AIR_VALUE, 0);

   nir_def *comps[NIR_MAX_VEC_COMPONENTS];
   struct air_type ety = ty;
   ety.components = 1;
   for (unsigned i = 0; i < ty.components; i++) {
      if (L.LLVMIsAUndefValue(c) || L.LLVMIsAConstantAggregateZero(c)) {
         comps[i] = air_scalar_constant(t, c, &ety);
      } else {
         LLVMValueRef e = L.LLVMGetAggregateElement(c, i);
         if (!e)
            unsupported(t, "a vector constant whose element %u LLVM will not give", i);
         comps[i] = air_scalar_constant(t, e, &ety);
      }
   }
   return air_set(t, c, nir_vec(&t->top, comps, ty.components), AIR_VALUE, 0);
}

static struct air_val *
air_get(struct air_tr *t, LLVMValueRef v)
{
   struct hash_entry *e = _mesa_hash_table_search(t->values, v);
   if (e)
      return e->data;
   if (L.LLVMIsAGlobalVariable(v))
      return air_global(t, v);
   if (air_is_aggregate(L.LLVMTypeOf(v))) {
      if (!L.LLVMIsAConstantAggregateZero(v) && !L.LLVMIsAUndefValue(v) && !L.LLVMIsAConstantStruct(v) &&
          !L.LLVMIsAConstantArray(v) && !L.LLVMIsAConstantDataSequential(v))
         unsupported(t, "an aggregate value used before it is defined (%s)", air_name(v, NULL));
      unsigned n = air_num_elems(t, L.LLVMTypeOf(v));
      struct air_val *r = air_aggregate(t, n);
      for (unsigned i = 0; i < n; i++) {
         LLVMValueRef c = L.LLVMGetAggregateElement(v, i);
         if (!c)
            unsupported(t, "an aggregate constant whose element %u LLVM will not give", i);
         r->elems[i] = air_get(t, c);
      }
      return air_set_val(t, v, r);
   }
   if (L.LLVMIsAConstantInt(v) || L.LLVMIsAConstantFP(v) || L.LLVMIsAConstantExpr(v) ||
       L.LLVMIsAConstantAggregateZero(v) || L.LLVMIsAConstantDataSequential(v) || L.LLVMIsAConstantVector(v) ||
       L.LLVMIsAConstantPointerNull(v) || L.LLVMIsAUndefValue(v))
      return air_constant(t, v);
   if (L.LLVMIsAFunction(v))
      unsupported(t, "the function %s used as a value (a function pointer)", air_name(v, NULL));
   unsupported(t, "a value used before it is defined (%s)", air_name(v, NULL));
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Globals: threadgroup memory and constant tables                                                                    */

static void
air_put_bytes(struct air_tr *t, uint8_t *out, uint64_t size, LLVMValueRef c, LLVMTypeRef ty)
{
   if (L.LLVMIsAUndefValue(c) || L.LLVMIsAConstantAggregateZero(c) || L.LLVMIsAConstantPointerNull(c)) {
      memset(out, 0, size);
      return;
   }
   switch (L.LLVMGetTypeKind(ty)) {
   case TY_INTEGER: {
      uint64_t u = L.LLVMConstIntGetZExtValue(c);
      unsigned bytes = (L.LLVMGetIntTypeWidth(ty) + 7) / 8;
      for (unsigned i = 0; i < bytes && i < size; i++)
         out[i] = (uint8_t)(u >> (8 * i));
      return;
   }
   case TY_HALF: case TY_FLOAT: {
      int loses = 0;
      double d = L.LLVMConstRealGetDouble(c, &loses);
      if (L.LLVMGetTypeKind(ty) == TY_HALF) {
         uint16_t h = _mesa_float_to_half((float)d);
         memcpy(out, &h, 2);
      } else {
         float f = (float)d;
         memcpy(out, &f, 4);
      }
      return;
   }
   case TY_ARRAY: case TY_VECTOR: {
      LLVMTypeRef ety = L.LLVMGetElementType(ty);
      uint64_t stride = air_size(t, ety);
      uint64_t n = stride ? size / stride : 0;
      if (L.LLVMGetTypeKind(ty) == TY_VECTOR)
         n = L.LLVMGetVectorSize(ty);
      for (uint64_t i = 0; i < n; i++) {
         LLVMValueRef e = L.LLVMGetAggregateElement(c, (unsigned)i);
         if (!e)
            unsupported(t, "a constant table whose element %llu LLVM will not give", (unsigned long long)i);
         air_put_bytes(t, out + i * stride, stride, e, ety);
      }
      return;
   }
   case TY_STRUCT: {
      unsigned n = L.LLVMCountStructElementTypes(ty);
      for (unsigned i = 0; i < n; i++) {
         LLVMTypeRef fty = L.LLVMStructGetTypeAtIndex(ty, i);
         uint64_t off = L.LLVMOffsetOfElement(t->layout, ty, i);
         LLVMValueRef e = L.LLVMGetAggregateElement(c, i);
         if (!e)
            unsupported(t, "a constant structure whose field %u LLVM will not give", i);
         air_put_bytes(t, out + off, air_size(t, fty), e, fty);
      }
      return;
   }
   default:
      unsupported(t, "a constant table holding a value of LLVM type kind %d", L.LLVMGetTypeKind(ty));
   }
}

static bool air_has_fc_values(const struct air_tr *t);
static bool air_fc_index(LLVMValueRef g, uint32_t *index);

static struct air_val *
air_global(struct air_tr *t, LLVMValueRef g)
{
   struct hash_entry *e = _mesa_hash_table_search(t->globals, g);
   if (e)
      return e->data;
   unsigned space = L.LLVMGetPointerAddressSpace(L.LLVMTypeOf(g));
   LLVMTypeRef vty = L.LLVMGlobalGetValueType(g);
   uint64_t size = air_size(t, vty);
   uint32_t align = MAX2(L.LLVMGetAlignment(g), L.LLVMABIAlignmentOfType(t->layout, vty));
   align = MAX2(align, 4);
   struct air_val *r = ralloc(t->mem, struct air_val);
   r->align = align;
   if (space == 3) {
      if (size > 64 * 1024)
         unsupported(t, "a threadgroup variable of %llu bytes", (unsigned long long)size);
      t->shared_size = ALIGN_POT(t->shared_size, align);
      r->def = nir_imm_int(&t->top, t->shared_size);
      r->kind = AIR_SHARED;
      t->shared_size += (uint32_t)size;
   } else if (space == 2) {
      LLVMValueRef init = L.LLVMGetInitializer(g);
      if (!init || L.LLVMIsDeclaration(g))
         unsupported(t, "the constant %s has no value here (a function constant?)", air_name(g, NULL));
      /* Only a constant table's initializer is its value. A function constant (its MTL_FC_INIT_ global is externally
       * initialized) and what Apple's compiler derives from them (__metal_implicit_fc_pred_*, the "...Defined"
       * flags: addrspace(2) globals that are not constant, written by the module's initializer) get their values
       * when the pipeline is specialized, which this request cannot say. */
      if (!L.LLVMIsGlobalConstant(g) || L.LLVMIsExternallyInitialized(g)) {
         uint32_t index;
         if (!air_has_fc_values(t))
            unsupported(t, "the kernel reads %s, a function constant or a value derived from one, and the request "
                        "has no function-constant values", air_name(g, NULL));
         if (air_fc_index(g, &index))
            unsupported(t, "the kernel reads function constant %u (%s), which the request does not give", index,
                        air_name(g, NULL));
         unsupported(t, "the kernel reads %s, which depends on a function constant the request does not give",
                     air_name(g, NULL));
      }
      if (size > 64 * 1024)
         unsupported(t, "a constant table of %llu bytes", (unsigned long long)size);
      unsigned at = ALIGN_POT(util_dynarray_num_elements(&t->constant_data, uint8_t), align);
      while (util_dynarray_num_elements(&t->constant_data, uint8_t) < at + size)
         util_dynarray_append_typed(&t->constant_data, uint8_t, 0);
      air_put_bytes(t, util_dynarray_element(&t->constant_data, uint8_t, at), size, init, vty);
      r->def = nir_imm_int(&t->top, at);
      r->kind = AIR_CONSTANT;
   } else {
      unsupported(t, "the global %s in address space %u", air_name(g, NULL), space);
   }
   _mesa_hash_table_insert(t->globals, g, r);
   return r;
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Pointers                                                                                                           */

static nir_def *
air_resize(nir_builder *b, nir_def *v, unsigned bits, bool sign)
{
   if (v->bit_size == bits)
      return v;
   return sign ? nir_i2iN(b, v, bits) : nir_u2uN(b, v, bits);
}

/* The byte offset a GEP's indices add, in the pointer's width; *align is narrowed to what stays known. */
static nir_def *
air_gep_offset(struct air_tr *t, nir_builder *b, LLVMValueRef gep, LLVMTypeRef ty, unsigned first, unsigned count,
               unsigned bits, uint32_t *align)
{
   uint64_t constant = 0;
   nir_def *offset = NULL;
   for (unsigned i = first; i < count; i++) {
      LLVMValueRef index = L.LLVMGetOperand(gep, i);
      uint64_t stride;
      if (i == first) {
         stride = air_size(t, ty);
      } else if (L.LLVMGetTypeKind(ty) == TY_STRUCT) {
         if (!L.LLVMIsAConstantInt(index))
            unsupported(t, "a structure field chosen at run time");
         unsigned field = (unsigned)L.LLVMConstIntGetZExtValue(index);
         constant += L.LLVMOffsetOfElement(t->layout, ty, field);
         ty = L.LLVMStructGetTypeAtIndex(ty, field);
         continue;
      } else if (L.LLVMGetTypeKind(ty) == TY_ARRAY || L.LLVMGetTypeKind(ty) == TY_VECTOR) {
         ty = L.LLVMGetElementType(ty);
         stride = air_size(t, ty);
      } else {
         unsupported(t, "a GEP into LLVM type kind %d", L.LLVMGetTypeKind(ty));
      }
      if (L.LLVMGetTypeKind(L.LLVMTypeOf(index)) == TY_VECTOR)
         unsupported(t, "a vector GEP");
      if (L.LLVMIsAConstantInt(index)) {
         constant += (uint64_t)L.LLVMConstIntGetSExtValue(index) * stride;
         continue;
      }
      nir_def *idx = air_resize(b, air_signed(b, air_def(t, index), air_width_of(t, index)), bits, true);
      nir_def *scaled = nir_imul_imm(b, idx, stride);
      offset = offset ? nir_iadd(b, offset, scaled) : scaled;
      *align = air_gcd_align(*align, stride);
   }
   *align = air_gcd_align(*align, constant);
   if (!offset)
      return constant ? nir_imm_intN_t(b, constant, bits) : NULL;
   return constant ? nir_iadd_imm(b, offset, constant) : offset;
}

static struct air_val *
air_gep(struct air_tr *t, nir_builder *b, LLVMValueRef gep, LLVMValueRef result_key)
{
   struct air_val *base = air_get(t, L.LLVMGetOperand(gep, 0));
   if (base->kind == AIR_VALUE)
      unsupported(t, "a GEP on a value that is not a pointer");
   if (base->kind == AIR_TEXARRAY) {
      /* into an array<texture, N> argument (its handles, 8 bytes each, each the one field of a metal::texture): the
       * element index */
      uint32_t align = 8;
      nir_def *off = air_gep_offset(t, b, gep, L.LLVMGetGEPSourceElementType(gep), 1, (unsigned)L.LLVMGetNumOperands(gep),
                                    32, &align);
      if (align % 8)
         unsupported(t, "a GEP into an array of textures that is not at one of its handles");
      nir_def *e = base->def;
      if (off && nir_src_is_const(nir_src_for_ssa(off)) && nir_src_is_const(nir_src_for_ssa(base->def)))
         /* a known element stays known: it is that texture's own binding */
         e = nir_imm_int(b, (int)(nir_src_as_uint(nir_src_for_ssa(base->def)) + (nir_src_as_uint(nir_src_for_ssa(off)) >> 3)));
      else if (off)
         e = nir_iadd(b, base->def, nir_ushr_imm(b, off, 3));
      struct air_val *r = air_set(t, result_key, e, AIR_TEXARRAY, 8);
      r->res = base->res;
      return r;
   }
   uint32_t align = base->align;
   unsigned bits = air_vdef(t, base)->bit_size;
   nir_def *off = air_gep_offset(t, b, gep, L.LLVMGetGEPSourceElementType(gep), 1,
                                 (unsigned)L.LLVMGetNumOperands(gep), bits, &align);
   nir_def *addr = off ? nir_iadd(b, base->def, off) : base->def;
   return air_set(t, result_key, addr, base->kind, align);
}

static struct air_val *
air_constant_expr(struct air_tr *t, LLVMValueRef c)
{
   switch (L.LLVMGetConstOpcode(c)) {
   case OP_GEP:
      return air_gep(t, &t->top, c, c);
   case OP_BITCAST: case OP_ADDRSPACECAST: {
      struct air_val *src = air_get(t, L.LLVMGetOperand(c, 0));
      struct air_val *r = air_set(t, c, src->def, src->kind, src->align);
      r->res = src->res;
      return r;
   }
   default:
      unsupported(t, "a constant expression with LLVM opcode %d", L.LLVMGetConstOpcode(c));
   }
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Memory access                                                                                                      */

/* The alignment a device or constant buffer access is emitted with: as LLVM gives it, or at most 4 bytes for the
 * plugin's align guard (NVMTL_AIR_NIR_ALIGN4), whose build runs when a buffer is bound at an address the kernel's
 * accesses are not aligned to; NAK turns a 16-byte promise into one 128-bit access, which faults on such an address.
 * The largest is reported, for the plugin to know whether to guard the kernel at all. */
static uint32_t
air_global_align(struct air_tr *t, uint32_t align)
{
   if ((t->req->flags & NVMTL_AIR_NIR_ALIGN4) && align > 4)
      align = 4;
   t->global_align = MAX2(t->global_align, align);
   return align;
}

static nir_def *
air_load_mem(struct air_tr *t, struct air_val *ptr, unsigned components, unsigned bits, uint32_t align)
{
   nir_builder *b = &t->b;
   align = MAX2(align, 1);
   if (!util_is_power_of_two_nonzero(align))
      align = 1u << (ffs(align) - 1);
   switch (ptr->kind) {
   case AIR_GLOBAL:
      align = air_global_align(t, align);
      return nir_load_global(b, components, bits, ptr->def, .align_mul = align);
   case AIR_CONSTANT:
      return nir_load_constant(b, components, bits, ptr->def, .base = 0, .range = ~0u, .align_mul = align);
   case AIR_SHARED:
      return nir_load_shared(b, components, bits, ptr->def, .base = 0, .align_mul = align);
   case AIR_SCRATCH:
      return nir_load_scratch(b, components, bits, ptr->def, .align_mul = align);
   default:
      unsupported(t, "a load through a value that is not a pointer");
   }
}

static void
air_store_mem(struct air_tr *t, struct air_val *ptr, nir_def *value, uint32_t align)
{
   nir_builder *b = &t->b;
   align = MAX2(align, 1);
   if (!util_is_power_of_two_nonzero(align))
      align = 1u << (ffs(align) - 1);
   switch (ptr->kind) {
   case AIR_GLOBAL:
      align = air_global_align(t, align);
      nir_store_global(b, value, ptr->def, .align_mul = align);
      return;
   case AIR_SHARED:
      nir_store_shared(b, value, ptr->def, .base = 0, .align_mul = align);
      return;
   case AIR_SCRATCH:
      nir_store_scratch(b, value, ptr->def, .align_mul = align);
      return;
   case AIR_CONSTANT:
      unsupported(t, "a store to constant memory");
   default:
      unsupported(t, "a store through a value that is not a pointer");
   }
}

/* A load of an LLVM type: bools are bytes in memory, thread and threadgroup pointers 64-bit words; a structure or an
 * array element by element, at the offsets of LLVM's data layout. */
static struct air_val *air_texarray_load(struct air_tr *t, struct air_val *ptr);

static struct air_val *
air_load_val(struct air_tr *t, struct air_val *ptr, LLVMTypeRef lty, uint32_t align)
{
   nir_builder *b = &t->b;
   if (ptr->kind == AIR_TEXARRAY) {
      if (L.LLVMGetTypeKind(lty) != TY_POINTER)
         unsupported(t, "a load from an array of textures that is not one of its handles");
      return air_texarray_load(t, ptr);
   }
   if (air_is_aggregate(lty)) {
      unsigned n = air_num_elems(t, lty);
      struct air_val *r = air_aggregate(t, n);
      for (unsigned i = 0; i < n; i++) {
         uint64_t off = air_elem_offset(t, lty, i);
         uint32_t a = air_gcd_align(align ? align : 1, off);
         struct air_val *at = air_make(t, off ? nir_iadd_imm(b, ptr->def, off) : ptr->def, ptr->kind, a);
         r->elems[i] = air_load_val(t, at, air_elem_type(lty, i), a);
      }
      return r;
   }
   struct air_type ty = air_type_of(t, lty);
   if (ty.width != ty.bits)
      unsupported(t, "a load of an i%u", ty.width);
   if (ty.is_pointer) {
      nir_def *v = air_load_mem(t, ptr, 1, 64, align);
      enum air_kind k = air_kind_of_space(ty.address_space);
      return air_make(t, k == AIR_GLOBAL ? v : nir_u2u32(b, v), k, 1);
   }
   if (ty.bits == 1) {
      nir_def *v = air_load_mem(t, ptr, ty.components, 8, align);
      return air_make(t, nir_ine_imm(b, v, 0), AIR_VALUE, 0);
   }
   return air_make(t, air_load_mem(t, ptr, ty.components, ty.bits, align), AIR_VALUE, 0);
}

static struct air_val *
air_load(struct air_tr *t, LLVMValueRef inst, struct air_val *ptr, LLVMTypeRef lty, uint32_t align)
{
   return air_set_val(t, inst, air_load_val(t, ptr, lty, align));
}

static void
air_store_val(struct air_tr *t, struct air_val *ptr, struct air_val *val, LLVMTypeRef lty, uint32_t align)
{
   nir_builder *b = &t->b;
   if (air_is_aggregate(lty)) {
      unsigned n = air_num_elems(t, lty);
      if (val->kind != AIR_AGGREGATE || val->num_elems != n)
         unsupported(t, "a store of an aggregate whose value is not one");
      for (unsigned i = 0; i < n; i++) {
         uint64_t off = air_elem_offset(t, lty, i);
         uint32_t a = air_gcd_align(align ? align : 1, off);
         struct air_val *at = air_make(t, off ? nir_iadd_imm(b, ptr->def, off) : ptr->def, ptr->kind, a);
         air_store_val(t, at, val->elems[i], air_elem_type(lty, i), a);
      }
      return;
   }
   struct air_type ty = air_type_of(t, lty);
   if (ty.width != ty.bits)
      unsupported(t, "a store of an i%u", ty.width);
   nir_def *v = air_vdef(t, val);
   if (ty.is_pointer)
      v = nir_u2u64(b, v);
   else if (ty.bits == 1)
      v = nir_b2iN(b, v, 8);
   air_store_mem(t, ptr, v, align);
}

static void
air_store(struct air_tr *t, struct air_val *ptr, LLVMValueRef value, uint32_t align)
{
   air_store_val(t, ptr, air_get(t, value), L.LLVMTypeOf(value), align);
}

/* llvm.memcpy / llvm.memmove / llvm.memset with a length known here, in the widest pieces both ends allow. */
static void
air_mem_copy(struct air_tr *t, LLVMValueRef call, bool set)
{
   nir_builder *b = &t->b;
   struct air_val *dst = air_get(t, L.LLVMGetOperand(call, 0));
   LLVMValueRef len = L.LLVMGetOperand(call, 2);
   if (!L.LLVMIsAConstantInt(len))
      unsupported(t, "a %s of a length known only at run time", set ? "memset" : "memcpy");
   uint64_t n = L.LLVMConstIntGetZExtValue(len);
   if (n > 4096)
      unsupported(t, "a %s of %llu bytes", set ? "memset" : "memcpy", (unsigned long long)n);
   struct air_val *src = NULL;
   nir_def *fill = NULL;
   uint32_t align = dst->align;
   if (set) {
      fill = air_def(t, L.LLVMGetOperand(call, 1));
      fill = nir_u2u32(b, fill);
      fill = nir_imul_imm(b, nir_iand_imm(b, fill, 0xff), 0x01010101);
   } else {
      src = air_get(t, L.LLVMGetOperand(call, 1));
      align = MIN2(align, src->align);
   }
   uint64_t at = 0;
   while (at < n) {
      unsigned piece = 16;
      while (piece > 1 && (piece > align || at + piece > n || (at % piece)))
         piece >>= 1;
      unsigned bits = piece >= 4 ? 32 : piece * 8;
      unsigned comps = piece >= 4 ? piece / 4 : 1;
      struct air_val d = { nir_iadd_imm(b, dst->def, at), dst->kind, piece };
      nir_def *v;
      if (set) {
         nir_def *word = bits == 32 ? fill : nir_u2uN(b, fill, bits);
         nir_def *c[4] = { word, word, word, word };
         v = nir_vec(b, c, comps);
      } else {
         struct air_val s = { nir_iadd_imm(b, src->def, at), src->kind, piece };
         v = air_load_mem(t, &s, comps, bits, piece);
      }
      air_store_mem(t, &d, v, piece);
      at += piece;
   }
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Kernel arguments and the dispatch grid                                                                             */

static nir_def *
air_push_word(struct air_tr *t, unsigned i)
{
   if (!t->push_word[i])
      t->push_word[i] = nir_load_deref(&t->top, nir_build_deref_struct(&t->top, nir_build_deref_var(&t->top, t->push), i));
   return t->push_word[i];
}

/* the table's descriptor, loaded once at the entry */
static void
air_table_desc(struct air_tr *t)
{
   nir_builder *b = &t->top;
   if (!t->table) {
      nir_def *index = nir_vulkan_resource_index(b, nir_address_format_num_components(t->table_format),
                                                 nir_address_format_bit_size(t->table_format), nir_imm_int(b, 0),
                                                 .desc_set = t->set, .binding = NVMTL_AIR_NIR_TABLE_BINDING,
                                                 .desc_type = t->table_mode == nir_var_mem_ubo ?
                                                    nir_descriptor_type_uniform_buffer :
                                                    nir_descriptor_type_storage_buffer);
      t->table = nir_load_vulkan_descriptor(b, nir_address_format_num_components(t->table_format),
                                            nir_address_format_bit_size(t->table_format), index,
                                            .desc_type = t->table_mode == nir_var_mem_ubo ?
                                               nir_descriptor_type_uniform_buffer :
                                               nir_descriptor_type_storage_buffer);
   }
}

static nir_def *
air_table_entry(struct air_tr *t, unsigned slot)
{
   nir_builder *b = &t->top;
   air_table_desc(t);
   nir_deref_instr *d = nir_build_deref_cast(b, t->table, t->table_mode, t->table_type, 0);
   d->cast.align_mul = 16;
   d->cast.align_offset = 0;
   d = nir_build_deref_array_imm(b, d, slot);
   t->buffer_mask |= 1u << slot;
   return nir_load_deref_with_access(b, d, ACCESS_NON_WRITEABLE | ACCESS_CAN_REORDER);
}

static nir_def *
air_num_workgroups(struct air_tr *t)
{
   if (!t->num_workgroups)
      t->num_workgroups = nir_load_num_workgroups(&t->top);
   return t->num_workgroups;
}

/* threads_per_grid (first 0) or threadgroups_per_grid (first 9), with the indirect-dispatch convention. */
static nir_def *
air_grid_field(struct air_tr *t, unsigned first)
{
   nir_builder *b = &t->top;
   nir_def *c[3];
   for (unsigned i = 0; i < 3; i++) {
      nir_def *w = air_push_word(t, first + i);
      nir_def *indirect = nir_ine_imm(b, nir_iand_imm(b, w, 0x80000000u), 0);
      nir_def *groups = nir_channel(b, air_num_workgroups(t), i);
      nir_def *gpu = first == 0 ? nir_imul(b, groups, nir_iand_imm(b, w, 0x7fffffffu)) : groups;
      c[i] = nir_bcsel(b, indirect, gpu, w);
   }
   return nir_vec(b, c, 3);
}

static nir_def *
air_builtin(struct air_tr *t, const char *kind)
{
   nir_builder *b = &t->top;
   if (!strcmp(kind, "air.thread_position_in_grid")) {
      nir_def *base = nir_vec3(b, air_push_word(t, 3), air_push_word(t, 4), air_push_word(t, 5));
      return nir_iadd(b, nir_load_global_invocation_id(b, 32), base);
   }
   if (!strcmp(kind, "air.threadgroup_position_in_grid")) {
      nir_def *base = nir_vec3(b, air_push_word(t, 6), air_push_word(t, 7), air_push_word(t, 8));
      return nir_iadd(b, nir_load_workgroup_id(b), base);
   }
   if (!strcmp(kind, "air.thread_position_in_threadgroup"))
      return nir_load_local_invocation_id(b);
   if (!strcmp(kind, "air.thread_index_in_threadgroup"))
      return nir_load_local_invocation_index(b);
   if (!strcmp(kind, "air.threads_per_threadgroup"))
      return nir_load_workgroup_size(b);
   if (!strcmp(kind, "air.threads_per_grid"))
      return air_grid_field(t, 0);
   if (!strcmp(kind, "air.threadgroups_per_grid"))
      return air_grid_field(t, 9);
   if (!strcmp(kind, "air.thread_index_in_simdgroup"))
      return nir_load_subgroup_invocation(b);
   if (!strcmp(kind, "air.threads_per_simdgroup") || !strcmp(kind, "air.thread_execution_width"))
      return nir_imm_int(b, 32);
   if (!strcmp(kind, "air.simdgroup_index_in_threadgroup"))
      return nir_load_subgroup_id(b);
   if (!strcmp(kind, "air.simdgroups_per_threadgroup"))
      return nir_load_num_subgroups(b);
   return NULL;
}

static unsigned
air_md_operands(LLVMValueRef node, LLVMValueRef **out, void *mem)
{
   unsigned n = L.LLVMGetMDNodeNumOperands(node);
   *out = rzalloc_array(mem, LLVMValueRef, n + 1);
   L.LLVMGetMDNodeOperands(node, *out);
   return n;
}

static const char *
air_md_string(LLVMValueRef v)
{
   if (!v || !L.LLVMIsAMDString(v))
      return NULL;
   unsigned len = 0;
   return L.LLVMGetMDString(v, &len);
}

static bool
air_md_int(LLVMValueRef v, uint64_t *out)
{
   if (!v || !L.LLVMIsAConstantInt(v))
      return false;
   *out = L.LLVMConstIntGetZExtValue(v);
   return true;
}

/* The entry's node in !air.kernel, !air.vertex or !air.fragment: {function, outputs, arguments, ...}. A vertex or
 * fragment function's node may say more after its arguments: a fragment function's early_fragment_tests is read, and
 * anything else (a post-tessellation vertex function's patch, ...) is refused. Mesh and object functions are refused. */
static LLVMValueRef
air_find_kernel(struct air_tr *t, LLVMValueRef *args_node)
{
   static const struct { const char *md; unsigned stage; } lists[] = {
      { "air.kernel", NVMTL_AIR_NIR_STAGE_KERNEL }, { "air.vertex", NVMTL_AIR_NIR_STAGE_VERTEX },
      { "air.fragment", NVMTL_AIR_NIR_STAGE_FRAGMENT }, { "air.mesh", ~0u }, { "air.object", ~0u },
   };
   LLVMValueRef found = NULL, *found_ops = NULL;
   unsigned total = 0, found_count = 0;
   for (unsigned l = 0; l < ARRAY_SIZE(lists); l++) {
      unsigned n = L.LLVMGetNamedMetadataNumOperands(t->module, lists[l].md);
      if (!n)
         continue;
      total += n;
      LLVMValueRef *nodes = rzalloc_array(t->mem, LLVMValueRef, n);
      L.LLVMGetNamedMetadataOperands(t->module, lists[l].md, nodes);
      for (unsigned i = 0; i < n; i++) {
         LLVMValueRef *ops;
         unsigned count = air_md_operands(nodes[i], &ops, t->mem);
         if (count < 3 || !ops[0] || !L.LLVMIsAFunction(ops[0]))
            continue;
         if (t->req->entry && strcmp(air_name(ops[0], NULL), t->req->entry))
            continue;
         if (found)
            air_refuse(t, NVMTL_AIR_NIR_BAD_REQUEST, "the module has more than one function and the request names none");
         found = ops[0];
         found_ops = ops;
         found_count = count;
         t->stage = lists[l].stage;
      }
   }
   if (!total)
      air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "the module has no !air.kernel, !air.vertex or !air.fragment");
   if (!found)
      air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "no function named %s", t->req->entry ? t->req->entry : "(any)");
   if (t->stage == ~0u)
      unsupported(t, "a mesh or object function");
   *args_node = found_ops[2];
   t->outputs_node = found_ops[1];
   if (t->stage != NVMTL_AIR_NIR_STAGE_KERNEL) {
      for (unsigned k = 3; k < found_count; k++) {
         const char *what = air_md_string(found_ops[k]);
         if (t->stage == NVMTL_AIR_NIR_STAGE_FRAGMENT && what && !strcmp(what, "early_fragment_tests"))
            t->fragment_flags |= NVMTL_AIR_NIR_FS_EARLY_TESTS;
         else
            unsupported(t, "a %s function with %s%s", t->stage == NVMTL_AIR_NIR_STAGE_VERTEX ? "vertex" : "fragment",
                        what ? what : "metadata this does not read (a post-tessellation vertex function's patch, ...)",
                        what ? " in its metadata" : "");
      }
   }
   return found;
}

/* What one !air.kernel argument node says. */
struct air_arg {
   LLVMValueRef param;
   const char *kind;
   uint64_t location, space, align, size;
   LLVMValueRef fc_node;   /* an argument that exists only under a function constant: its {global, type, name} */
   const char *type_name;  /* air.arg_type_name */
   const char *access;     /* air.read, air.write, air.read_write, air.sample */
   const char *location_unknown;   /* the index is a global no value is known for (a function constant's) */
   const char *semantic;   /* a varying's user semantic: user(x), or the compiler's generated(...) */
   const char *arg_name;   /* air.arg_name */
   bool flat, no_perspective, centroid, at_sample;   /* a fragment input's interpolation */
   bool raster_order_group;
   bool sized;             /* air.buffer_size: a block of a fixed size, not an array the data indexes */
   uint64_t rt_index;      /* a [[color(n)]] input: n */
   const char *rt_unknown; /* ... a global no value is known for (a function constant's) */
};

static struct air_arg
air_read_arg(struct air_tr *t, LLVMValueRef node, unsigned i, unsigned params)
{
   LLVMValueRef *ops;
   unsigned count = air_md_operands(node, &ops, t->mem);
   uint64_t index;
   struct air_arg a = { .location = ~0ull, .space = ~0ull };
   a.kind = count >= 2 ? air_md_string(ops[1]) : NULL;
   if (!a.kind || !air_md_int(ops[0], &index) || index >= params)
      air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "argument %u of the kernel has malformed metadata", i);
   a.param = L.LLVMGetParam(t->function, (unsigned)index);
   /* an argument that exists only when a function constant is true: {index, "air.function_constant", constant, kind,
    * ...}; it is bound as its kind says (a kernel that reads the constant itself is refused where it does) */
   unsigned first = 2;
   if (!strcmp(a.kind, "air.function_constant") && count >= 4 && air_md_string(ops[3])) {
      a.kind = air_md_string(ops[3]);
      a.fc_node = ops[2];
      first = 4;
   }
   /* a [[color(n)]] input's n follows its kind: {index, "air.render_target", n, ...}, n a literal or a global Apple's
    * compiler computes from function constants, which the static initializer filled (as for a colour output) */
   if (!strcmp(a.kind, "air.render_target") && first < count) {
      if (!air_md_int(ops[first], &a.rt_index)) {
         LLVMValueRef g = ops[first];
         while (g && L.LLVMIsAConstantExpr(g))
            g = L.LLVMGetOperand(g, 0);
         LLVMValueRef init = g && L.LLVMIsAGlobalVariable(g) && L.LLVMIsGlobalConstant(g) &&
                             !L.LLVMIsExternallyInitialized(g) ? L.LLVMGetInitializer(g) : NULL;
         if (init && L.LLVMIsAConstantInt(init))
            a.rt_index = L.LLVMConstIntGetZExtValue(init);
         else
            a.rt_unknown = g ? air_name(g, NULL) : "(?)";
      }
      first++;
   }
   /* a varying's semantic follows its kind: {index, "air.fragment_input", "user(x)", ...} */
   if ((!strcmp(a.kind, "air.fragment_input") || !strcmp(a.kind, "air.vertex_output")) && first < count &&
       air_md_string(ops[first]) && strncmp(air_md_string(ops[first]), "air.", 4))
      a.semantic = air_md_string(ops[first++]);
   for (unsigned k = first; k < count; k++) {
      const char *key = air_md_string(ops[k]);
      if (!key)
         continue;
      if (!strcmp(key, "air.read") || !strcmp(key, "air.write") || !strcmp(key, "air.read_write") ||
          !strcmp(key, "air.sample"))
         a.access = key;
      a.flat |= !strcmp(key, "air.flat");
      a.no_perspective |= !strcmp(key, "air.no_perspective");
      a.centroid |= !strcmp(key, "air.centroid");
      a.at_sample |= !strcmp(key, "air.sample");
      a.raster_order_group |= !strcmp(key, "air.raster_order_group");
      if (k + 1 < count && !strcmp(key, "air.arg_name"))
         a.arg_name = air_md_string(ops[k + 1]);
      if (k + 1 >= count)
         continue;
      if (!strcmp(key, "air.arg_type_name"))
         a.type_name = air_md_string(ops[k + 1]);
      if (!strcmp(key, "air.location_index") && !air_md_int(ops[k + 1], &a.location)) {
         /* an index Apple's compiler computes from function constants: a global the static initializer fills */
         LLVMValueRef g = ops[k + 1];
         while (g && L.LLVMIsAConstantExpr(g))
            g = L.LLVMGetOperand(g, 0);
         LLVMValueRef init = g && L.LLVMIsAGlobalVariable(g) && L.LLVMIsGlobalConstant(g) &&
                             !L.LLVMIsExternallyInitialized(g) ? L.LLVMGetInitializer(g) : NULL;
         if (!init || !L.LLVMIsAConstantInt(init))
            a.location_unknown = g ? air_name(g, NULL) : "(?)";   /* refused if the argument exists */
         else
            a.location = L.LLVMConstIntGetZExtValue(init);
      }
      else if (!strcmp(key, "air.address_space"))
         air_md_int(ops[k + 1], &a.space);
      else if (!strcmp(key, "air.arg_type_align_size"))
         air_md_int(ops[k + 1], &a.align);
      else if (!strcmp(key, "air.arg_type_size"))
         air_md_int(ops[k + 1], &a.size);
      else if (!strcmp(key, "air.buffer_size"))
         a.sized = true;
   }
   /* OpenCL's by-value constant arguments are constant buffers at their index */
   if (!strcmp(a.kind, "air.constant")) {
      a.kind = "air.buffer";
      a.space = 2;
   }
   /* a buffer whose node has no air.address_space (the GL-to-Metal compiler's): the parameter's pointer type says it,
    * as the translator reads addrspace(N) off the define line (an address space 0 pointer stays refused below) */
   if (!strcmp(a.kind, "air.buffer") && a.space == ~0ull && a.param &&
       L.LLVMGetTypeKind(L.LLVMTypeOf(a.param)) == TY_POINTER && L.LLVMGetPointerAddressSpace(L.LLVMTypeOf(a.param)))
      a.space = L.LLVMGetPointerAddressSpace(L.LLVMTypeOf(a.param));
   /* [[threadgroup(n)]] pointers are buffers in address space 3 */
   if (!strcmp(a.kind, "air.buffer") && a.space == 3)
      a.kind = "air.threadgroup";
   return a;
}

/* Whether an argument guarded by a function constant exists: 1 yes, 0 no, -1 not known (the request has no values,
 * or its predicate depends on one it does not give). */
static int
air_fc_arg_present(struct air_tr *t, const struct air_arg *a)
{
   if (!a->fc_node)
      return 1;
   if (!air_has_fc_values(t) || !L.LLVMIsAMDNode(a->fc_node))
      return -1;
   LLVMValueRef *ops;
   unsigned n = air_md_operands(a->fc_node, &ops, t->mem);
   LLVMValueRef g = n ? ops[0] : NULL;
   while (g && L.LLVMIsAConstantExpr(g))
      g = L.LLVMGetOperand(g, 0);
   if (!g || !L.LLVMIsAGlobalVariable(g) || !L.LLVMIsGlobalConstant(g) || L.LLVMIsExternallyInitialized(g))
      return -1;
   LLVMValueRef init = L.LLVMGetInitializer(g);
   if (!init || !L.LLVMIsAConstantInt(init))
      return -1;
   return L.LLVMConstIntGetZExtValue(init) != 0;
}

struct air_texture;
struct air_sampler;
struct air_texarray;
static struct air_texture *air_bind_texture(struct air_tr *t, const struct air_arg *a, unsigned i);
static bool air_texarray_type(struct air_tr *t, const char *name, char **elem, unsigned *count);
static struct air_texarray *air_bind_texarray(struct air_tr *t, const struct air_arg *a, unsigned i, const char *elem,
                                              unsigned count);
static struct air_sampler *air_bind_sampler(struct air_tr *t, const struct air_arg *a, unsigned i);
static void air_assign_static_samplers(struct air_tr *t);

/* ---------------------------------------------------------------------------------------------------------------- */
/* Vertex and fragment functions: stage inputs, builtins and outputs                                                  */
/*                                                                                                                    */
/* As the translator's stages read and write them: a vertex input [[attribute(n)]] at Location n (the vertex          */
/* descriptor's), the builtins as Vulkan's, and the varyings at the Locations the request gives by user semantic, or  */
/* the translator's own order without them. Every varying travels as 32-bit components (a half as a float, a narrow   */
/* integer or a bool as a 32-bit integer, flat), which both stages agree on because both come from here; a vertex     */
/* input is declared with 32-bit components too, as Vulkan delivers it. System values are variables, as               */
/* spirv_to_nir declares them, so NVK lowers them exactly as it lowers the SPIR-V road's.                             */

static bool
air_has_varyings(const struct air_tr *t)
{
   return (t->req->flags & NVMTL_AIR_NIR_VARYINGS) &&
          t->req->size >= offsetof(struct nvmtl_air_nir_request, varying_locations) + sizeof t->req->varying_locations;
}

/* The Location of the varying named key (own: its air.location_index, or ~0) */
static unsigned
air_varying_location(struct air_tr *t, const char *key, uint64_t own)
{
   uint64_t loc = ~0ull;
   if (air_has_varyings(t)) {
      for (uint32_t i = 0; i < t->req->varying_count; i++) {
         if (!key || !t->req->varying_keys || !t->req->varying_keys[i] || strcmp(t->req->varying_keys[i], key))
            continue;
         if (loc != ~0ull)
            unsupported(t, "the request names the varying %s twice", key);
         loc = t->req->varying_locations ? t->req->varying_locations[i] : ~0ull;
      }
      if (loc == ~0ull)
         unsupported(t, "the varying %s has no Location in the request", key ? key : "(unnamed)");
   } else {
      loc = own != ~0ull ? own : t->next_varying;
      t->next_varying++;
   }
   if (loc >= 32)
      unsupported(t, "the varying %s at Location %llu", key ? key : "(unnamed)", (unsigned long long)loc);
   if (t->varyings & (1u << loc))
      unsupported(t, "two varyings at Location %llu", (unsigned long long)loc);
   t->varyings |= 1u << loc;
   return (unsigned)loc;
}

/* The 32-bit components a stage input or output of AIR type ty travels in; the type's name tells a signed integer
 * from an unsigned one, which LLVM's types do not */
static enum glsl_base_type
air_io_base(const struct air_type *ty, const char *type_name)
{
   if (ty->is_float)
      return GLSL_TYPE_FLOAT;
   const char *n = type_name ? type_name : "";
   if (!strncmp(n, "packed_", 7))
      n += 7;
   return n[0] == 'u' || !strncmp(n, "bool", 4) ? GLSL_TYPE_UINT : GLSL_TYPE_INT;
}

static nir_def *
air_io_widen(nir_builder *b, nir_def *v, enum glsl_base_type base)
{
   if (base == GLSL_TYPE_FLOAT)
      return v->bit_size == 32 ? v : nir_f2f32(b, v);
   if (v->bit_size == 1)
      return nir_b2i32(b, v);
   if (v->bit_size == 32)
      return v;
   return base == GLSL_TYPE_INT ? nir_i2i32(b, v) : nir_u2u32(b, v);
}

static nir_def *
air_io_narrow(nir_builder *b, nir_def *v, const struct air_type *ty)
{
   if (ty->bits == 1)
      return v->bit_size == 1 ? v : nir_ine_imm(b, v, 0);
   if (v->bit_size == 1)
      return nir_b2iN(b, v, ty->bits);
   if (ty->bits == v->bit_size)
      return v;
   return ty->is_float ? nir_f2fN(b, v, ty->bits) : nir_u2uN(b, v, ty->bits);
}

static nir_def *
air_sysval(struct air_tr *t, gl_system_value sv, const struct glsl_type *type)
{
   nir_variable *var = NULL;
   unsigned i = 0;
   for (; i < ARRAY_SIZE(t->sysval) && t->sysval[i]; i++)
      if (t->sysval[i]->data.location == (int)sv)
         var = t->sysval[i];
   if (!var) {
      if (i == ARRAY_SIZE(t->sysval))
         unsupported(t, "more than %u system values", (unsigned)ARRAY_SIZE(t->sysval));
      var = t->sysval[i] = nir_variable_create(t->nir, nir_var_system_value, type, gl_system_value_name(sv));
      var->data.location = sv;
   }
   return nir_load_var(&t->top, var);
}

static nir_def *air_f2f16(nir_builder *b, nir_def *x);

/* A vertex or fragment function's argument that is a stage input or a builtin: true when a is one */
static bool
air_bind_stage_input(struct air_tr *t, const struct air_arg *a, unsigned i)
{
   nir_builder *b = &t->top;
   const bool vs = t->stage == NVMTL_AIR_NIR_STAGE_VERTEX, fs = t->stage == NVMTL_AIR_NIR_STAGE_FRAGMENT;
   const char *k = a->kind;
   const char *name = a->arg_name ? a->arg_name : "(unnamed)";
   LLVMTypeRef lty = L.LLVMTypeOf(a->param);
   if ((vs && !strcmp(k, "air.vertex_input")) || (fs && !strcmp(k, "air.fragment_input"))) {
      /* one that exists only under a function constant: the request's values say whether it does, as they do for an
       * output (the translator's specialization drops one whose constant is false: no Location, no attribute) */
      const int present = air_fc_arg_present(t, a);
      if (present < 0)
         unsupported(t, "the stage input %s exists only under a function constant %s", name,
                     air_has_fc_values(t) ? "the request does not give" : "(the request has no function-constant values)");
      if (air_is_aggregate(lty))
         unsupported(t, "the stage input %s of type %s (more than one Location)", name, a->type_name ? a->type_name : "?");
      struct air_type ty = air_type_of(t, lty);
      if (ty.is_pointer || ty.components > 4 || ty.bits > 32)
         unsupported(t, "the stage input %s of type %s", name, a->type_name ? a->type_name : "?");
      const enum glsl_base_type base = air_io_base(&ty, a->type_name);
      if (!present) {
         /* what reads it is folded away under the same constant: zeros, nothing declared */
         air_set(t, a->param, nir_imm_zero(b, ty.components, ty.bits), AIR_VALUE, 0);
         return true;
      }
      nir_variable *var;
      if (vs) {
         if (a->location >= 32)
            unsupported(t, "the vertex input %s at attribute %llu", name, (unsigned long long)a->location);
         const unsigned loc = (unsigned)a->location;
         if (t->vertex_inputs & (1u << loc))
            unsupported(t, "two vertex inputs at attribute %u", loc);
         t->vertex_inputs |= 1u << loc;
         t->vertex_input_kind[loc] = base == GLSL_TYPE_FLOAT ? 1 : base == GLSL_TYPE_INT ? 2 : 3;
         var = nir_variable_create(t->nir, nir_var_shader_in, glsl_vector_type(base, ty.components), name);
         var->data.location = VERT_ATTRIB_GENERIC0 + loc;
      } else {
         const unsigned loc = air_varying_location(t, a->semantic ? a->semantic : a->arg_name, ~0ull);
         var = nir_variable_create(t->nir, nir_var_shader_in, glsl_vector_type(base, ty.components), name);
         var->data.location = VARYING_SLOT_VAR0 + loc;
         var->data.interpolation = base != GLSL_TYPE_FLOAT || a->flat ? INTERP_MODE_FLAT :
                                   a->no_perspective ? INTERP_MODE_NOPERSPECTIVE : INTERP_MODE_SMOOTH;
         var->data.centroid = a->centroid;
         var->data.sample = a->at_sample;
         if (a->at_sample)
            t->fragment_flags |= NVMTL_AIR_NIR_FS_PER_SAMPLE;
      }
      air_set(t, a->param, air_io_narrow(b, nir_load_var(b, var), &ty), AIR_VALUE, 0);
      return true;
   }
   if (fs && !strcmp(k, "air.render_target") && (t->req->flags & NVMTL_AIR_NIR_COLOR_INPUTS)) {
      /* [[color(n)]] read, framebuffer fetch (version 6), as the translator's stage-input pass reads it
       * (passes/stage_input/mod.rs, resources/collapse.rs adapt_input_attachment_read): an input attachment (SubpassData,
       * not multisampled) at set 3, binding 192 + n, InputAttachmentIndex n, read once at the entry with OpImageRead at
       * offset (0, 0) as a vec4 of 32-bit floats (for a float or a half input) or of unsigned 32-bit integers (LLVM's
       * integers are unsigned there), then cut to the argument's components and narrowed to a half exactly. The NIR is
       * what spirv_to_nir and vk_nir_lower_input_attachment_loads make of that, so NVK lowers it as it lowers the
       * SPIR-V road's. Every one declared is in result.color_inputs, used or not, as the module's decorations are. */
      const int present = air_fc_arg_present(t, a);
      if (present <= 0)
         unsupported(t, "the [[color(n)]] input %s exists only under a function constant %s", name,
                     present < 0 ? "the request does not give" : "that is false");
      if (a->rt_unknown)
         unsupported(t, "the [[color(n)]] input %s's n is %s, which depends on a function constant %s", name, a->rt_unknown,
                     air_has_fc_values(t) ? "the request does not give" : "(the request has no function-constant values)");
      if (a->rt_index >= 8)
         unsupported(t, "a [[color(%llu)]] input", (unsigned long long)a->rt_index);
      if (air_is_aggregate(lty))
         unsupported(t, "the [[color(n)]] input %s of type %s", name, a->type_name ? a->type_name : "?");
      const struct air_type ty = air_type_of(t, lty);
      if (ty.is_pointer || ty.components > 4 || (ty.is_float ? ty.bits != 16 && ty.bits != 32 : ty.bits != 32))
         unsupported(t, "the [[color(n)]] input %s of type %s (the translator reads 32-bit floats or integers, and halves)",
                     name, a->type_name ? a->type_name : "?");
      const unsigned n = (unsigned)a->rt_index;
      const enum glsl_base_type base = ty.is_float ? GLSL_TYPE_FLOAT : GLSL_TYPE_UINT;
      nir_variable *var = nir_variable_create(t->nir, nir_var_image, glsl_image_type(GLSL_SAMPLER_DIM_SUBPASS, false, base), name);
      var->data.descriptor_set = NVMTL_AIR_NIR_FETCH_SET;
      var->data.binding = NVMTL_AIR_NIR_FETCH_BINDING + n;
      var->data.index = n;
      var->data.image.format = PIPE_FORMAT_NONE;
      nir_def *v = nir_image_deref_input_attachment_load(b, 4, 32, &nir_build_deref_var(b, var)->def, nir_imm_ivec2(b, 0, 0),
                                                         nir_undef(b, 1, 32), nir_imm_int(b, 0),
                                                         .image_dim = GLSL_SAMPLER_DIM_SUBPASS, .image_array = false,
                                                         .dest_type = ty.is_float ? nir_type_float32 : nir_type_uint32,
                                                         .base = n, .range = 1);
      if (ty.components < 4)
         v = nir_trim_vector(b, v, ty.components);
      if (ty.is_float && ty.bits == 16)
         v = air_f2f16(b, v);
      t->color_inputs |= 1u << n;
      air_set(t, a->param, v, AIR_VALUE, 0);
      return true;
   }
   static const struct {
      unsigned stage;
      const char *kind;
      gl_system_value sv;
      unsigned components;
      bool is_bool, is_float;
   } builtins[] = {
      { NVMTL_AIR_NIR_STAGE_VERTEX, "air.vertex_id", SYSTEM_VALUE_VERTEX_ID, 1 },
      { NVMTL_AIR_NIR_STAGE_VERTEX, "air.instance_id", SYSTEM_VALUE_INSTANCE_INDEX, 1 },
      { NVMTL_AIR_NIR_STAGE_VERTEX, "air.base_vertex", SYSTEM_VALUE_FIRST_VERTEX, 1 },   /* Vulkan's BaseVertex */
      { NVMTL_AIR_NIR_STAGE_VERTEX, "air.base_instance", SYSTEM_VALUE_BASE_INSTANCE, 1 },
      { NVMTL_AIR_NIR_STAGE_FRAGMENT, "air.position", SYSTEM_VALUE_FRAG_COORD, 4, false, true },
      { NVMTL_AIR_NIR_STAGE_FRAGMENT, "air.front_facing", SYSTEM_VALUE_FRONT_FACE, 1, true },
      { NVMTL_AIR_NIR_STAGE_FRAGMENT, "air.point_coord", SYSTEM_VALUE_POINT_COORD, 2, false, true },
      { NVMTL_AIR_NIR_STAGE_FRAGMENT, "air.sample_id", SYSTEM_VALUE_SAMPLE_ID, 1 },
      { NVMTL_AIR_NIR_STAGE_FRAGMENT, "air.sample_mask_in", SYSTEM_VALUE_SAMPLE_MASK_IN, 1 },
      { NVMTL_AIR_NIR_STAGE_FRAGMENT, "air.render_target_array_index", SYSTEM_VALUE_LAYER_ID, 1 },
   };
   for (unsigned j = 0; j < ARRAY_SIZE(builtins); j++) {
      if (builtins[j].stage != t->stage || strcmp(builtins[j].kind, k))
         continue;
      if (air_is_aggregate(lty))
         unsupported(t, "%s as an aggregate", k);
      struct air_type ty = air_type_of(t, lty);
      if (ty.is_pointer || ty.is_float != builtins[j].is_float || ty.components > builtins[j].components ||
          (builtins[j].is_float && ty.components != builtins[j].components))
         unsupported(t, "%s of type %s", k, a->type_name ? a->type_name : "?");
      const struct glsl_type *gt = builtins[j].is_bool ? glsl_bool_type() :
                                   builtins[j].is_float ? glsl_vec_type(builtins[j].components) :
                                   builtins[j].sv == SYSTEM_VALUE_SAMPLE_MASK_IN ? glsl_int_type() : glsl_uint_type();
      nir_def *v = air_sysval(t, builtins[j].sv, gt);
      if (builtins[j].sv == SYSTEM_VALUE_SAMPLE_ID)
         t->fragment_flags |= NVMTL_AIR_NIR_FS_PER_SAMPLE;
      if (ty.components < v->num_components)
         v = nir_trim_vector(b, v, ty.components);
      air_set(t, a->param, air_io_narrow(b, v, &ty), AIR_VALUE, 0);
      (void)i;
      return true;
   }
   return false;
}

/* A vertex or fragment function's return value: one output variable per member its outputs node describes */
static void
air_bind_outputs(struct air_tr *t)
{
   const bool vs = t->stage == NVMTL_AIR_NIR_STAGE_VERTEX;
   LLVMTypeRef rty = L.LLVMGetReturnType(L.LLVMGlobalGetValueType(t->function));
   LLVMValueRef *nodes = NULL;
   const unsigned n = t->outputs_node && L.LLVMIsAMDNode(t->outputs_node) ? air_md_operands(t->outputs_node, &nodes, t->mem) : 0;
   const bool is_struct = L.LLVMGetTypeKind(rty) == TY_STRUCT;
   const unsigned members = L.LLVMGetTypeKind(rty) == TY_VOID ? 0 : is_struct ? L.LLVMCountStructElementTypes(rty) : 1;
   if (members != n)
      unsupported(t, "a return value of %u members that its metadata describes as %u outputs", members, n);
   t->outputs = rzalloc_array(t->mem, struct air_output, n + 1);
   t->num_outputs = n;
   for (unsigned i = 0; i < n; i++) {
      LLVMValueRef *ops;
      unsigned count = air_md_operands(nodes[i], &ops, t->mem);
      const char *kind = count ? air_md_string(ops[0]) : NULL;
      if (!kind)
         air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "output %u of the function has malformed metadata", i);
      /* an output that exists only when a function constant is true: {"air.function_constant", {gate}, kind, ...}; the
       * request's values say whether it does (the translator drops one whose constant is false) */
      if (!strcmp(kind, "air.function_constant")) {
         const struct air_arg gate = { .fc_node = count > 1 ? ops[1] : NULL };
         const char *inner = count > 2 ? air_md_string(ops[2]) : NULL;
         if (!inner || !gate.fc_node)
            air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "output %u of the function has malformed metadata", i);
         const int present = air_fc_arg_present(t, &gate);
         if (present < 0)
            unsupported(t, "the %s output %s exists only under a function constant the request does not give",
                        vs ? "vertex" : "fragment", inner);
         if (!present) {
            t->outputs[i].dropped = true;
            continue;
         }
         ops += 2;
         count -= 2;
         kind = inner;
      }
      const char *semantic = NULL, *type_name = NULL, *name = NULL, *qualifier = NULL;
      uint64_t location = ~0ull, clip = 0, rt = ~0ull, rt_index = 0;
      unsigned first = 1;
      if (!strcmp(kind, "air.vertex_output") && count > 1 && air_md_string(ops[1]) && strncmp(air_md_string(ops[1]), "air.", 4))
         semantic = air_md_string(ops[first++]);
      if (!strcmp(kind, "air.render_target")) {
         /* {"air.render_target", n, [index,] ...}: n a literal, or a global Apple's compiler computes from function
          * constants ([[color(expr)]]), which the static initializer filled; the dual-source index is there or not */
         if (count < 2)
            air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "colour output %u has malformed metadata", i);
         if (!air_md_int(ops[1], &rt)) {
            LLVMValueRef g = ops[1];
            while (g && L.LLVMIsAConstantExpr(g))
               g = L.LLVMGetOperand(g, 0);
            LLVMValueRef init = g && L.LLVMIsAGlobalVariable(g) && L.LLVMIsGlobalConstant(g) && !L.LLVMIsExternallyInitialized(g)
                                ? L.LLVMGetInitializer(g) : NULL;
            if (!init || !L.LLVMIsAConstantInt(init))
               unsupported(t, "colour output %u's [[color(n)]] is %s, which depends on a function constant %s", i,
                           g ? air_name(g, NULL) : "(?)", air_has_fc_values(t) ? "the request does not give" :
                           "(the request has no function-constant values)");
            rt = L.LLVMConstIntGetZExtValue(init);
         }
         first = 2;
         if (count >= 3 && air_md_int(ops[2], &rt_index))
            first = 3;
      }
      for (unsigned k = first; k + 1 < count; k++) {
         const char *key = air_md_string(ops[k]);
         if (!key)
            continue;
         if (!strcmp(key, "air.arg_type_name"))
            type_name = air_md_string(ops[k + 1]);
         else if (!strcmp(key, "air.arg_name"))
            name = air_md_string(ops[k + 1]);
         else if (!strcmp(key, "air.depth_qualifier"))
            qualifier = air_md_string(ops[k + 1]);
         else if (!strcmp(key, "air.location_index"))
            air_md_int(ops[k + 1], &location);
         else if (!strcmp(key, "air.clip_distance_array_size"))
            air_md_int(ops[k + 1], &clip);
      }
      LLVMTypeRef ety = is_struct ? L.LLVMStructGetTypeAtIndex(rty, i) : rty;
      struct air_output *o = &t->outputs[i];
      const char *vname = name ? name : kind;
      if (vs && !strcmp(kind, "air.clip_distance")) {
         uint64_t len = L.LLVMGetTypeKind(ety) == TY_ARRAY ? L.LLVMGetArrayLength2(ety) : 1;
         LLVMTypeRef el = L.LLVMGetTypeKind(ety) == TY_ARRAY ? L.LLVMGetElementType(ety) : ety;
         if (L.LLVMGetTypeKind(el) != TY_FLOAT || !len || len > 8 || (clip && clip != len))
            unsupported(t, "clip distances of this type (%s)", type_name ? type_name : "?");
         o->array = L.LLVMGetTypeKind(ety) == TY_ARRAY ? (unsigned)len : 0;
         o->base = GLSL_TYPE_FLOAT;
         o->components = 1;
         o->var = nir_variable_create(t->nir, nir_var_shader_out, glsl_array_type(glsl_float_type(), (unsigned)len, 0), vname);
         o->var->data.location = VARYING_SLOT_CLIP_DIST0;
         o->var->data.compact = true;
         t->nir->info.clip_distance_array_size = (unsigned)len;
         if (!o->array)
            o->array = 1;   /* a single float: element 0 */
         continue;
      }
      if (air_is_aggregate(ety))
         unsupported(t, "the output %s of type %s (more than one Location)", vname, type_name ? type_name : "?");
      struct air_type ty = air_type_of(t, ety);
      if (ty.is_pointer || ty.components > 4 || ty.bits > 32)
         unsupported(t, "the output %s of type %s", vname, type_name ? type_name : "?");
      o->base = air_io_base(&ty, type_name);
      o->components = ty.components;
      int slot = -1;
      if (vs) {
         if (!strcmp(kind, "air.position") && ty.is_float && ty.components == 4)
            slot = VARYING_SLOT_POS;
         else if (!strcmp(kind, "air.point_size") && ty.is_float && ty.components == 1)
            slot = VARYING_SLOT_PSIZ;
         else if (!strcmp(kind, "air.render_target_array_index") && !ty.is_float && ty.components == 1)
            slot = VARYING_SLOT_LAYER;
         else if (!strcmp(kind, "air.viewport_array_index") && !ty.is_float && ty.components == 1)
            slot = VARYING_SLOT_VIEWPORT;
         else if (!strcmp(kind, "air.vertex_output"))
            slot = VARYING_SLOT_VAR0 + air_varying_location(t, semantic ? semantic : name, location);
         else
            unsupported(t, "the vertex output %s (%s)", kind, type_name ? type_name : "?");
      } else {
         if (!strcmp(kind, "air.render_target")) {
            if (rt >= 8)
               unsupported(t, "[[color(%llu)]]", (unsigned long long)rt);
            if (rt_index)
               unsupported(t, "a dual-source colour output ([[color(%llu), index(%llu)]])", (unsigned long long)rt,
                           (unsigned long long)rt_index);
            if (t->color_outputs & (1u << rt))
               unsupported(t, "two outputs of [[color(%llu)]]", (unsigned long long)rt);
            t->color_outputs |= 1u << rt;
            slot = FRAG_RESULT_DATA0 + (int)rt;
         } else if (!strcmp(kind, "air.depth") && ty.is_float && ty.components == 1) {
            slot = FRAG_RESULT_DEPTH;
            t->fragment_flags |= NVMTL_AIR_NIR_FS_DEPTH;
            t->nir->info.fs.depth_layout = !qualifier || !strcmp(qualifier, "air.any") ? FRAG_DEPTH_LAYOUT_ANY :
                                           !strcmp(qualifier, "air.greater") ? FRAG_DEPTH_LAYOUT_GREATER :
                                           !strcmp(qualifier, "air.less") ? FRAG_DEPTH_LAYOUT_LESS : FRAG_DEPTH_LAYOUT_ANY;
         } else if (!strcmp(kind, "air.sample_mask") && !ty.is_float && ty.components == 1) {
            slot = FRAG_RESULT_SAMPLE_MASK;
            t->fragment_flags |= NVMTL_AIR_NIR_FS_SAMPLE_MASK;
            t->nir->info.fs.sample_mask_out_declared = true;
            o->base = GLSL_TYPE_INT;
         } else
            unsupported(t, "the fragment output %s (%s)", kind, type_name ? type_name : "?");
      }
      o->var = nir_variable_create(t->nir, nir_var_shader_out, glsl_vector_type(o->base, o->components), vname);
      o->var->data.location = slot;
      if (!vs && o->base != GLSL_TYPE_FLOAT && slot >= FRAG_RESULT_DATA0)
         o->var->data.interpolation = INTERP_MODE_FLAT;
   }
}

/* At a return: each member of the value into its output */
static void
air_store_outputs(struct air_tr *t, LLVMValueRef value)
{
   nir_builder *b = &t->b;
   if (!t->num_outputs)
      return;
   struct air_val *v = air_get(t, value);
   const bool whole = L.LLVMGetTypeKind(L.LLVMTypeOf(value)) != TY_STRUCT;
   for (unsigned i = 0; i < t->num_outputs; i++) {
      struct air_output *o = &t->outputs[i];
      if (o->dropped)
         continue;
      struct air_val *e = whole ? v : (v->kind == AIR_AGGREGATE && i < v->num_elems) ? v->elems[i] : NULL;
      if (!e)
         unsupported(t, "a return value of another shape than the function's");
      if (o->array) {
         const bool single = o->array == 1 && e->kind != AIR_AGGREGATE;
         if (!single && (e->kind != AIR_AGGREGATE || e->num_elems != o->array))
            unsupported(t, "clip distances of another shape than declared");
         for (unsigned k = 0; k < o->array; k++) {
            struct air_val *c = single ? e : e->elems[k];
            if (c->kind != AIR_VALUE || !c->def)
               unsupported(t, "a clip distance that is not a value");
            nir_store_deref(b, nir_build_deref_array_imm(b, nir_build_deref_var(b, o->var), k),
                            air_io_widen(b, c->def, GLSL_TYPE_FLOAT), 1);
         }
         continue;
      }
      if (e->kind != AIR_VALUE || !e->def || e->def->num_components != o->components)
         unsupported(t, "an output that is not a value of its type");
      nir_store_var(b, o->var, air_io_widen(b, e->def, o->base), BITFIELD_MASK(o->components));
   }
}

/* Whether v depends on data read from memory or a texture (a load, a texture read, an atomic, a call that is not math),
 * as opposed to constants, arguments (built-ins, stage inputs, buffer pointers) and arithmetic on them: the
 * translator's test (translator/wrapper/src/buffer_addresses.rs data_dependent). insts holds the function's
 * instructions; out of budget is taken as data. */
static bool air_starts(const char *s, const char *prefix);

static bool
air_data_dependent(struct air_tr *t, struct set *insts, struct set *seen, LLVMValueRef v, unsigned *budget)
{
   if (!v || !_mesa_set_search(insts, v) || _mesa_set_search(seen, v))
      return false;
   _mesa_set_add(seen, v);
   if (!*budget)
      return true;
   (*budget)--;
   switch (L.LLVMGetInstructionOpcode(v)) {
   case OP_LOAD:
      return true;
   case OP_CALL: {
      LLVMValueRef callee = L.LLVMGetCalledValue(v);
      const char *name = callee && L.LLVMIsAFunction(callee) ? air_name(callee, NULL) : "";
      if (strstr(name, "texture") || strstr(name, "atomic") || strstr(name, "sample") || strstr(name, "gather") ||
          (!air_starts(name, "air.") && !air_starts(name, "llvm.")))
         return true;
      for (unsigned i = 0; i < L.LLVMGetNumArgOperands(v); i++)
         if (air_data_dependent(t, insts, seen, L.LLVMGetOperand(v, i), budget))
            return true;
      return false;
   }
   default:
      for (int i = 0; i < L.LLVMGetNumOperands(v); i++)
         if (air_data_dependent(t, insts, seen, L.LLVMGetOperand(v, i), budget))
            return true;
      return false;
   }
}

/* A vertex or fragment function that indexes a buffer argument holding an array (no air.buffer_size) with a value
 * read from memory is refused: the translator keeps such a buffer a descriptor (buffer_addresses.rs kept_roots), which
 * the device bounds-checks for graphics pipelines (robustBufferAccess), because the data may point past its end (a
 * light list, a froxel grid) and a read there through the buffer's address is an MMU fault that takes the device away.
 * The road reads every buffer through its address, so such a function takes the SPIR-V road. Compute pipelines are not
 * bounds-checked on either road. */
static void
air_refuse_unchecked_indexing(struct air_tr *t, const struct air_arg *args, unsigned n)
{
   if (t->stage == NVMTL_AIR_NIR_STAGE_KERNEL)
      return;
   struct set *roots = _mesa_pointer_set_create(t->mem), *insts = _mesa_pointer_set_create(t->mem);
   for (unsigned i = 0; i < n; i++)
      if (args[i].kind && !strcmp(args[i].kind, "air.buffer") && !args[i].sized && args[i].param)
         _mesa_set_add(roots, args[i].param);
   if (!roots->entries)
      return;
   for (LLVMBasicBlockRef bb = L.LLVMGetFirstBasicBlock(t->function); bb; bb = L.LLVMGetNextBasicBlock(bb))
      for (LLVMValueRef i = L.LLVMGetFirstInstruction(bb); i; i = L.LLVMGetNextInstruction(i))
         _mesa_set_add(insts, i);
   set_foreach(insts, e) {
      LLVMValueRef gep = (LLVMValueRef)e->key;
      if (L.LLVMGetInstructionOpcode(gep) != OP_GEP)
         continue;
      /* the buffer the address is in: through GEPs and casts of pointers back to the argument */
      LLVMValueRef base = L.LLVMGetOperand(gep, 0);
      for (unsigned d = 0; d < 32 && base && _mesa_set_search(insts, base); d++) {
         const int op = L.LLVMGetInstructionOpcode(base);
         if (op != OP_GEP && op != OP_BITCAST && op != OP_ADDRSPACECAST)
            break;
         base = L.LLVMGetOperand(base, 0);
      }
      if (!base || !_mesa_set_search(roots, base))
         continue;
      for (int k = 1; k < L.LLVMGetNumOperands(gep); k++) {
         unsigned budget = 512;
         struct set *seen = _mesa_pointer_set_create(t->mem);
         if (air_data_dependent(t, insts, seen, L.LLVMGetOperand(gep, k), &budget))
            unsupported(t, "a buffer of the %s function is indexed with a value read from memory or a texture: the "
                        "SPIR-V road keeps that buffer bounds-checked (robustBufferAccess), and a read past its end "
                        "through its address is a GPU fault", t->stage == NVMTL_AIR_NIR_STAGE_VERTEX ? "vertex" : "fragment");
      }
   }
}

/* Buffers read their address from the table, builtins from the system values; threadgroup arguments take the
 * first bytes of threadgroup memory, in index order, at the lengths the request gives, and the kernel's static
 * threadgroup variables follow them as the instructions meet them. */
static void
air_bind_arguments(struct air_tr *t, LLVMValueRef args_node)
{
   LLVMValueRef *nodes;
   unsigned n = args_node && L.LLVMIsAMDNode(args_node) ? air_md_operands(args_node, &nodes, t->mem) : 0;
   unsigned params = L.LLVMCountParams(t->function);
   struct air_arg *args = rzalloc_array(t->mem, struct air_arg, n + 1);
   uint32_t tg_offsets[NVMTL_AIR_NIR_MAX_BUFFERS] = { 0 };
   uint32_t tg_len[NVMTL_AIR_NIR_MAX_BUFFERS] = { 0 };
   bool tg_used[NVMTL_AIR_NIR_MAX_BUFFERS] = { false };

   for (unsigned i = 0; i < n; i++) {
      struct air_arg *a = &args[i];
      *a = air_read_arg(t, nodes[i], i, params);
      if (a->location_unknown && air_fc_arg_present(t, a) != 0)
         unsupported(t, "argument %u's index is %s, which depends on a function constant %s", i, a->location_unknown,
                     air_has_fc_values(t) ? "the request does not give" : "(the request has no function-constant values)");
      if (a->raster_order_group)
         unsupported(t, "argument %u is in a raster order group", i);
      if (t->stage != NVMTL_AIR_NIR_STAGE_KERNEL && air_bind_stage_input(t, a, i))
         continue;
      if (!strcmp(a->kind, "air.buffer")) {
         if (!air_fc_arg_present(t, a)) {
            /* a function constant leaves it out: nothing is bound at its slot, and what read it was folded away */
            air_set(t, a->param, nir_imm_int64(&t->top, 0), AIR_GLOBAL, 16);
            continue;
         }
         if (a->location >= NVMTL_AIR_NIR_MAX_BUFFERS)
            unsupported(t, "buffer argument %u has no buffer index below %u", i, NVMTL_AIR_NIR_MAX_BUFFERS);
         if (a->space != 1 && a->space != 2)
            unsupported(t, "buffer argument %u in address space %llu", i, (unsigned long long)a->space);
         if (L.LLVMGetTypeKind(L.LLVMTypeOf(a->param)) != TY_POINTER)
            unsupported(t, "buffer argument %u is not a pointer", i);
         air_set(t, a->param, air_table_entry(t, (unsigned)a->location), AIR_GLOBAL,
                 a->align ? MIN2((uint32_t)a->align, 16) : 4);
      } else if (!strcmp(a->kind, "air.texture")) {
         char *elem = NULL;
         unsigned count = 0;
         if ((t->req->flags & NVMTL_AIR_NIR_TEXTURE_ARRAYS) && air_texarray_type(t, a->type_name, &elem, &count)) {
            struct air_val *v = air_set(t, a->param, nir_imm_int(&t->top, 0), AIR_TEXARRAY, 8);
            v->res = air_bind_texarray(t, a, i, elem, count);
            continue;
         }
         struct air_val *v = air_set(t, a->param, NULL, AIR_TEXTURE, 1);
         v->res = air_bind_texture(t, a, i);
      } else if (!strcmp(a->kind, "air.sampler")) {
         struct air_val *v = air_set(t, a->param, NULL, AIR_SAMPLER, 1);
         v->res = air_bind_sampler(t, a, i);
      } else if (!strcmp(a->kind, "air.threadgroup")) {
         if (t->stage != NVMTL_AIR_NIR_STAGE_KERNEL)
            unsupported(t, "a threadgroup argument of a vertex or fragment function");
         if (a->location >= NVMTL_AIR_NIR_MAX_BUFFERS)
            unsupported(t, "threadgroup argument %u has no index below %u", i, NVMTL_AIR_NIR_MAX_BUFFERS);
         uint32_t len = t->req->threadgroup_lengths[a->location];
         /* without a length, the translator's default (stage_input workgroup_array_elements): 512 elements of the
          * argument's type, as many as fit in 32 KB, at least one */
         if (!len && (t->req->flags & NVMTL_AIR_NIR_TG_DEFAULT) && a->size && a->size <= 32768)
            len = (uint32_t)(MAX2(MIN2(512ull, 32768ull / a->size), 1ull) * a->size);
         if (!len)
            unsupported(t, "threadgroup argument %llu has no length in the request",
                        (unsigned long long)a->location);
         tg_len[a->location] = len;
         tg_used[a->location] = true;
      } else {
         /* a vertex or fragment function reads only the SIMD-group builtins among the kernel's (Metal allows no more) */
         if (t->stage != NVMTL_AIR_NIR_STAGE_KERNEL && strcmp(a->kind, "air.thread_index_in_simdgroup") &&
             strcmp(a->kind, "air.threads_per_simdgroup") && strcmp(a->kind, "air.thread_execution_width"))
            unsupported(t, "the %s argument kind %s", t->stage == NVMTL_AIR_NIR_STAGE_VERTEX ? "vertex" : "fragment", a->kind);
         nir_def *v = air_builtin(t, a->kind);
         if (!v)
            unsupported(t, "the kernel argument kind %s", a->kind);
         struct air_type ty = air_type_of(t, L.LLVMTypeOf(a->param));
         if (ty.is_float || ty.is_pointer || ty.bits < 16)
            unsupported(t, "%s as a %s", a->kind, ty.is_float ? "float" : "pointer or narrow integer");
         if (ty.components < v->num_components)
            v = nir_trim_vector(&t->top, v, ty.components);
         else if (ty.components > v->num_components)
            unsupported(t, "%s with %u components", a->kind, ty.components);
         air_set(t, a->param, air_resize(&t->top, v, ty.bits, false), AIR_VALUE, 0);
      }
   }

   uint32_t at = 0;
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++) {
      if (!tg_used[i])
         continue;
      at = ALIGN_POT(at, 16);
      tg_offsets[i] = at;
      at += tg_len[i];
   }
   t->shared_size = at;
   for (unsigned i = 0; i < n; i++) {
      if (!strcmp(args[i].kind, "air.threadgroup"))
         air_set(t, args[i].param, nir_imm_int(&t->top, tg_offsets[args[i].location]), AIR_SHARED, 16);
   }

   for (unsigned p = 0; p < params; p++) {
      if (!_mesa_hash_table_search(t->values, L.LLVMGetParam(t->function, p)))
         unsupported(t, "parameter %u of the function has no description in its metadata", p);
   }
   air_assign_static_samplers(t);
   air_refuse_unchecked_indexing(t, args, n);
   if (t->stage != NVMTL_AIR_NIR_STAGE_KERNEL)
      air_bind_outputs(t);
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Arithmetic                                                                                                         */

static nir_def *
air_to_bits(nir_builder *b, nir_def *v, unsigned bits)
{
   return v->bit_size == bits ? v : nir_u2uN(b, v, bits);
}

static nir_def *
air_binop(struct air_tr *t, int op, nir_def *x, nir_def *y)
{
   nir_builder *b = &t->b;
   bool boolean = x->bit_size == 1;
   switch (op) {
   case OP_ADD: return nir_iadd(b, x, y);
   case OP_SUB: return nir_isub(b, x, y);
   case OP_MUL: return nir_imul(b, x, y);
   case OP_UDIV: return nir_udiv(b, x, y);
   case OP_SDIV: return nir_idiv(b, x, y);
   case OP_UREM: return nir_umod(b, x, y);
   case OP_SREM: return nir_irem(b, x, y);
   case OP_SHL: return nir_ishl(b, x, air_to_bits(b, y, 32));
   case OP_LSHR: return nir_ushr(b, x, air_to_bits(b, y, 32));
   case OP_ASHR: return nir_ishr(b, x, air_to_bits(b, y, 32));
   case OP_AND: return boolean ? nir_iand(b, x, y) : nir_iand(b, x, y);
   case OP_OR: return nir_ior(b, x, y);
   case OP_XOR: return nir_ixor(b, x, y);
   case OP_FADD: return nir_fadd(b, x, y);
   case OP_FSUB: return nir_fsub(b, x, y);
   case OP_FMUL: return nir_fmul(b, x, y);
   case OP_FDIV: return nir_fdiv(b, x, y);
   case OP_FREM: return nir_frem(b, x, y);
   default: unsupported(t, "LLVM binary opcode %d", op);
   }
}

static nir_def *
air_icmp(struct air_tr *t, int pred, nir_def *x, nir_def *y)
{
   nir_builder *b = &t->b;
   switch (pred) {
   case ICMP_EQ: return nir_ieq(b, x, y);
   case ICMP_NE: return nir_ine(b, x, y);
   case ICMP_UGT: return nir_ult(b, y, x);
   case ICMP_UGE: return nir_uge(b, x, y);
   case ICMP_ULT: return nir_ult(b, x, y);
   case ICMP_ULE: return nir_uge(b, y, x);
   case ICMP_SGT: return nir_ilt(b, y, x);
   case ICMP_SGE: return nir_ige(b, x, y);
   case ICMP_SLT: return nir_ilt(b, x, y);
   case ICMP_SLE: return nir_ige(b, y, x);
   default: unsupported(t, "integer comparison %d", pred);
   }
}

static nir_def *
air_fcmp(struct air_tr *t, int pred, nir_def *x, nir_def *y)
{
   nir_builder *b = &t->b;
   switch (pred) {
   case FCMP_FALSE: return nir_imm_boolN_t(b, false, 1);
   case FCMP_TRUE: return nir_imm_boolN_t(b, true, 1);
   case FCMP_OEQ: return nir_feq(b, x, y);
   case FCMP_OGT: return nir_flt(b, y, x);
   case FCMP_OGE: return nir_fge(b, x, y);
   case FCMP_OLT: return nir_flt(b, x, y);
   case FCMP_OLE: return nir_fge(b, y, x);
   case FCMP_ONE: return nir_fneo(b, x, y);
   case FCMP_ORD: return nir_ford(b, x, y);
   case FCMP_UNO: return nir_funord(b, x, y);
   case FCMP_UEQ: return nir_fequ(b, x, y);
   case FCMP_UGT: return nir_fltu(b, y, x);
   case FCMP_UGE: return nir_fgeu(b, x, y);
   case FCMP_ULT: return nir_fltu(b, x, y);
   case FCMP_ULE: return nir_fgeu(b, y, x);
   case FCMP_UNE: return nir_fneu(b, x, y);
   default: unsupported(t, "float comparison %d", pred);
   }
}


/* min and max as the translator makes them (passes/air_calls/bfloat_glsl.rs nan_aware_if_precise): air.fmin, air.min,
 * air.clamp ... and llvm.minnum/maxnum are GLSL's NMin/NMax/NClamp, NaN-aware (the operand that is a number), and
 * spirv_to_nir keeps NaN and infinities for them; the fast_ ones are FMin/FMax/FClamp, which keep infinities only. The
 * builder's fast math would otherwise let NIR take either operand for a number. */
static nir_def *
air_fminmax(nir_builder *b, bool fast, bool max, nir_def *x, nir_def *y)
{
   const uint32_t save = b->fp_math_ctrl;
   b->fp_math_ctrl |= nir_fp_preserve_inf | (fast ? 0 : nir_fp_preserve_nan);
   nir_def *r = max ? nir_fmax(b, x, y) : nir_fmin(b, x, y);
   b->fp_math_ctrl = save;
   return r;
}

/* Metal's round, half away from zero, as the translator makes it (float_imageblock.rs lower_metal_round): the
 * truncation plus one toward the sign when the remainder reaches a half; a half is rounded in float. sign(x) *
 * floor(|x| + 0.5) rounded 0.49999997 to 1 and 2^23 + 1 to 2^23 + 2, as the sum rounds. */
static nir_def *
air_metal_round(nir_builder *b, nir_def *x)
{
   nir_def *w = x->bit_size == 32 ? x : nir_f2f32(b, x);
   nir_def *tr = nir_ftrunc(b, w), *rem = nir_fsub(b, w, tr);
   nir_def *adj = nir_bcsel(b, nir_fge_imm(b, rem, 0.5), nir_imm_float(b, 1.0),
                            nir_bcsel(b, nir_fle_imm(b, rem, -0.5), nir_imm_float(b, -1.0), nir_imm_float(b, 0.0)));
   nir_def *r = nir_fadd(b, tr, adj);
   return x->bit_size == 32 ? r : nir_f2fN(b, r, x->bit_size);
}

/* Metal's pow (air.pow and air.fast_pow; not powr, which is for bases of at least 0) as the translator makes it
 * (rawbyte_unary.rs lower_metal_pow), in float: |x|^y, negated for a negative base (or -0) and an odd integer exponent;
 * 1 for a base of +-1 and an exponent that is not finite; NaN for a finite negative base and an exponent that is not an
 * integer; 1 for an exponent of 0. NIR's fpow alone is exp2(y * log2(x)), NaN for every negative base. */
static nir_def *
air_metal_pow(nir_builder *b, nir_def *x0, nir_def *y0)
{
   nir_def *x = x0->bit_size == 32 ? x0 : nir_f2f32(b, x0), *y = y0->bit_size == 32 ? y0 : nir_f2f32(b, y0);
   nir_def *ax = nir_fabs(b, x), *m = nir_fpow(b, ax, y);
   nir_def *integral = nir_feq(b, y, nir_ftrunc(b, y));
   nir_def *odd = nir_iand(b, integral, nir_fneu(b, nir_fmul_imm(b, nir_ftrunc(b, nir_fmul_imm(b, y, 0.5)), 2.0), y));
   nir_def *neg = nir_flt_imm(b, x, 0.0);
   nir_def *neg_zero = nir_iand(b, nir_feq_imm(b, ax, 0.0), nir_ilt_imm(b, x, 0));
   nir_def *r = nir_bcsel(b, nir_iand(b, nir_ior(b, neg, neg_zero), odd), nir_fneg(b, m), m);
   nir_def *inf = nir_imm_float(b, INFINITY);
   r = nir_bcsel(b, nir_iand(b, nir_feq_imm(b, ax, 1.0), nir_inot(b, nir_flt(b, nir_fabs(b, y), inf))), nir_imm_float(b, 1.0), r);
   r = nir_bcsel(b, nir_iand(b, nir_iand(b, neg, nir_fneu(b, ax, inf)), nir_inot(b, integral)), nir_imm_float(b, NAN), r);
   r = nir_bcsel(b, nir_feq_imm(b, y, 0.0), nir_imm_float(b, 1.0), r);
   return x0->bit_size == 32 ? r : nir_f2fN(b, r, x0->bit_size);
}

/* A float narrowed to a half, exact as the translator marks it (finalize.rs exact_narrowing_float_conversions,
 * NoContraction): NIR does not fold it into the arithmetic around it */
static nir_def *
air_f2f16(nir_builder *b, nir_def *x)
{
   const uint32_t save = b->fp_math_ctrl;
   b->fp_math_ctrl |= nir_fp_exact;
   nir_def *r = nir_f2f16_rtne(b, x);
   b->fp_math_ctrl = save;
   return r;
}

/* x != x, kept as a NaN test: the builder makes fast-math instructions (nir_fp_fast_math), and with NaN not preserved
 * NIR may take x for a number and the test for false (nir_search_helpers.h is_a_number), as spirv_to_nir keeps
 * OpIsNan with nir_fp_preserve_nan */
static nir_def *
air_fisnan(nir_builder *b, nir_def *x)
{
   const uint32_t save = b->fp_math_ctrl;
   b->fp_math_ctrl |= nir_fp_preserve_nan | nir_fp_preserve_inf;
   nir_def *r = nir_fneu(b, x, x);
   b->fp_math_ctrl = save;
   return r;
}

/* A float converted to an integer as Metal converts it and as the translator does (passes/finalize.rs
 * metal_nan_to_zero_conversions): a NaN gives 0. NAK's F2I gives the type's minimum (0x80000000) for a NaN, and as a
 * buffer index that is an address 2 GiB below the buffer: CoreAnimation's buffer_image_blit_frag_lpf reads one byte at
 * int(x + offset + y * bpr) and faulted the GPU (Xid 31) in Terminal on the first NaN. A value out of range clamps to
 * the type's limit as F2I clamps it, as on the SPIR-V road. */
static nir_def *
air_f2int(nir_builder *b, nir_def *x, unsigned bits, bool is_signed)
{
   nir_def *r = is_signed ? nir_f2iN(b, x, bits) : nir_f2uN(b, x, bits);
   return nir_bcsel(b, air_fisnan(b, x), nir_imm_zero(b, r->num_components, bits), r);
}

static nir_def *
air_cast(struct air_tr *t, int op, nir_def *x, const struct air_type *to)
{
   nir_builder *b = &t->b;
   unsigned bits = to->bits;
   switch (op) {
   case OP_TRUNC:
      if (bits == 1)
         return nir_ine_imm(b, nir_iand_imm(b, x, 1), 0);
      return nir_u2uN(b, x, bits);
   case OP_ZEXT:
      return x->bit_size == 1 ? nir_b2iN(b, x, bits) : nir_u2uN(b, x, bits);
   case OP_SEXT:
      return x->bit_size == 1 ? nir_ineg(b, nir_b2iN(b, x, bits)) : nir_i2iN(b, x, bits);
   case OP_FPTOUI: return air_f2int(b, x, bits, false);
   case OP_FPTOSI: return air_f2int(b, x, bits, true);
   case OP_UITOFP: return x->bit_size == 1 ? nir_b2fN(b, x, bits) : nir_u2fN(b, x, bits);
   case OP_SITOFP: return x->bit_size == 1 ? nir_fneg(b, nir_b2fN(b, x, bits)) : nir_i2fN(b, x, bits);
   case OP_FPTRUNC: return bits == 16 ? air_f2f16(b, x) : nir_f2fN(b, x, bits);
   case OP_FPEXT: return nir_f2fN(b, x, bits);
   default: unsupported(t, "LLVM cast opcode %d", op);
   }
}

static nir_def *
air_bitcast(struct air_tr *t, nir_def *x, const struct air_type *to)
{
   nir_builder *b = &t->b;
   if (x->bit_size == to->bits && x->num_components == to->components)
      return x;
   if (x->bit_size == 1 || to->bits == 1)
      unsupported(t, "a bitcast of bools");
   if (to->width != to->bits || x->bit_size * x->num_components != to->bits * to->components)
      unsupported(t, "a bitcast between types of different sizes here (i%u)", to->width);
   return nir_bitcast_vector(b, x, to->bits);
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Calls: AIR's builtins and LLVM's intrinsics                                                                        */

static bool
air_starts(const char *s, const char *prefix)
{
   return !strncmp(s, prefix, strlen(prefix));
}

/* Transcendental functions of half operands run in single precision. */
static nir_def *
air_f32_unary(nir_builder *b, nir_op op, nir_def *x)
{
   if (x->bit_size == 32)
      return nir_build_alu1(b, op, x);
   return nir_f2fN(b, nir_build_alu1(b, op, nir_f2f32(b, x)), x->bit_size);
}

static nir_def *
air_widen(nir_builder *b, nir_def *x)
{
   return x->bit_size == 32 ? x : nir_f2f32(b, x);
}

static nir_def *
air_narrow(nir_builder *b, nir_def *x, unsigned bits)
{
   return bits == 32 ? x : nir_f2fN(b, x, bits);
}

static nir_def *
air_float_function(struct air_tr *t, const char *fn, nir_def **s, unsigned n, const struct air_type *rt)
{
   nir_builder *b = &t->b;
   nir_def *x = n > 0 ? s[0] : NULL;
#define IS(name) (!strcmp(fn, name))
#define NEED(k) do { if (n != (k)) unsupported(t, "air.%s with %u operands", fn, n); } while (0)
   if (IS("dfdx") || IS("dfdy") || IS("fwidth")) {
      NEED(1);
      if (t->stage != NVMTL_AIR_NIR_STAGE_FRAGMENT)
         unsupported(t, "air.%s outside a fragment function", fn);
      nir_def *w = air_widen(b, x);
      nir_def *r = IS("dfdx") ? nir_ddx(b, w) : IS("dfdy") ? nir_ddy(b, w)
                              : nir_fadd(b, nir_fabs(b, nir_ddx(b, w)), nir_fabs(b, nir_ddy(b, w)));
      return air_narrow(b, r, x->bit_size);
   }
   if (IS("fabs") || IS("abs")) { NEED(1); return nir_fabs(b, x); }
   if (IS("saturate")) { NEED(1); return nir_fsat(b, x); }
   if (IS("floor")) { NEED(1); return nir_ffloor(b, x); }
   if (IS("ceil")) { NEED(1); return nir_fceil(b, x); }
   if (IS("trunc")) { NEED(1); return nir_ftrunc(b, x); }
   if (IS("rint")) { NEED(1); return nir_fround_even(b, x); }
   if (IS("round")) { NEED(1); return air_metal_round(b, x); }
   if (IS("fract")) { NEED(1); return nir_ffract(b, x); }
   if (IS("sign")) { NEED(1); return nir_fsign(b, x); }
   if (IS("sqrt")) { NEED(1); return air_f32_unary(b, nir_op_fsqrt, x); }
   if (IS("rsqrt")) { NEED(1); return air_f32_unary(b, nir_op_frsq, x); }
   if (IS("exp2")) { NEED(1); return air_f32_unary(b, nir_op_fexp2, x); }
   if (IS("log2")) { NEED(1); return air_f32_unary(b, nir_op_flog2, x); }
   if (IS("sin")) { NEED(1); return air_f32_unary(b, nir_op_fsin, x); }
   if (IS("cos")) { NEED(1); return air_f32_unary(b, nir_op_fcos, x); }
   if (IS("exp") || IS("exp10") || IS("log") || IS("log10") || IS("tan") || IS("sinpi") || IS("cospi") ||
       IS("tanpi") || IS("sinh") || IS("cosh") || IS("tanh") || IS("asin") || IS("acos") || IS("atan")) {
      NEED(1);
      nir_def *w = air_widen(b, x), *r;
      if (IS("exp")) r = nir_fexp2(b, nir_fmul_imm(b, w, M_LOG2E));
      else if (IS("exp10")) r = nir_fexp2(b, nir_fmul_imm(b, w, 3.32192809488736234787));
      else if (IS("log")) r = nir_fmul_imm(b, nir_flog2(b, w), M_LN2);
      else if (IS("log10")) r = nir_fmul_imm(b, nir_flog2(b, w), 0.30102999566398119521);
      else if (IS("tan")) r = nir_fdiv(b, nir_fsin(b, w), nir_fcos(b, w));
      else if (IS("sinpi")) r = nir_fsin(b, nir_fmul_imm(b, w, M_PI));
      else if (IS("cospi")) r = nir_fcos(b, nir_fmul_imm(b, w, M_PI));
      else if (IS("tanpi")) { nir_def *a = nir_fmul_imm(b, w, M_PI); r = nir_fdiv(b, nir_fsin(b, a), nir_fcos(b, a)); }
      else if (IS("sinh") || IS("cosh") || IS("tanh")) {
         nir_def *ep = nir_fexp2(b, nir_fmul_imm(b, w, M_LOG2E));
         nir_def *en = nir_fexp2(b, nir_fmul_imm(b, w, -M_LOG2E));
         nir_def *sh = nir_fmul_imm(b, nir_fsub(b, ep, en), 0.5), *ch = nir_fmul_imm(b, nir_fadd(b, ep, en), 0.5);
         if (IS("tanh")) {
            /* exp overflows past |x| ~ 88: tanh is +-1 there */
            r = nir_bcsel(b, nir_flt_imm(b, nir_fabs(b, w), 20.0), nir_fdiv(b, sh, ch), nir_fsign(b, w));
         } else {
            r = IS("sinh") ? sh : ch;
         }
      }
      else if (IS("atan")) r = nir_atan(b, w);
      else {
         nir_def *c = nir_fsqrt(b, nir_fmax(b, nir_fsub(b, nir_imm_float(b, 1.0), nir_fmul(b, w, w)), nir_imm_float(b, 0.0)));
         r = IS("asin") ? nir_atan2(b, w, c) : nir_atan2(b, c, w);
      }
      return air_narrow(b, r, x->bit_size);
   }
   if (IS("pow")) { NEED(2); return air_metal_pow(b, s[0], s[1]); }
   if (IS("powr")) { NEED(2); return air_narrow(b, nir_fpow(b, air_widen(b, s[0]), air_widen(b, s[1])), x->bit_size); }
   if (IS("atan2")) { NEED(2); return air_narrow(b, nir_atan2(b, air_widen(b, s[0]), air_widen(b, s[1])), x->bit_size); }
   if (IS("fmax") || IS("max")) { NEED(2); return air_fminmax(b, t->math_fast, true, s[0], s[1]); }
   if (IS("fmin") || IS("min")) { NEED(2); return air_fminmax(b, t->math_fast, false, s[0], s[1]); }
   if (IS("fmod")) { NEED(2); return nir_frem(b, s[0], s[1]); }
   if (IS("fdim")) { NEED(2); return nir_fmax(b, nir_fsub(b, s[0], s[1]), nir_imm_floatN_t(b, 0.0, x->bit_size)); }
   if (IS("copysign")) {
      NEED(2);
      uint64_t sign = 1ull << (x->bit_size - 1);
      return nir_ior(b, nir_iand_imm(b, s[0], ~sign & BITFIELD64_MASK(x->bit_size)), nir_iand_imm(b, s[1], sign));
   }
   if (IS("ldexp")) {
      /* air.[fast_]ldexp(x, int e): GLSL's Ldexp, f32 only as the translator lowers it (lower_fast_ldexp), a scalar
       * exponent of a vector splatted */
      NEED(2);
      nir_def *e = s[1];
      if (x->bit_size != 32 || e->bit_size != 32 || (e->num_components != 1 && e->num_components != x->num_components))
         unsupported(t, "air.%s of a %u-bit value with a %u-bit exponent", fn, x->bit_size, e->bit_size);
      return nir_ldexp(b, x, e->num_components == x->num_components ? e : nir_replicate(b, e, x->num_components));
   }
   if (IS("fma")) { NEED(3); return nir_ffma(b, s[0], s[1], s[2]); }
   if (IS("clamp") || IS("fclamp")) { NEED(3); return air_fminmax(b, t->math_fast, false, air_fminmax(b, t->math_fast, true, s[0], s[1]), s[2]); }
   if (IS("mix")) { NEED(3); return nir_flrp(b, s[0], s[1], s[2]); }
   if (IS("fmax3")) { NEED(3); return air_fminmax(b, t->math_fast, true, air_fminmax(b, t->math_fast, true, s[0], s[1]), s[2]); }
   if (IS("fmin3")) { NEED(3); return air_fminmax(b, t->math_fast, false, air_fminmax(b, t->math_fast, false, s[0], s[1]), s[2]); }
   if (IS("fmedian3")) {
      NEED(3);
      const bool f = t->math_fast;
      return air_fminmax(b, f, true, air_fminmax(b, f, false, s[0], s[1]), air_fminmax(b, f, false, air_fminmax(b, f, true, s[0], s[1]), s[2]));
   }
   if (IS("dot")) {
      NEED(2);
      switch (x->num_components) {
      case 2: return nir_fdot2(b, s[0], s[1]);
      case 3: return nir_fdot3(b, s[0], s[1]);
      case 4: return nir_fdot4(b, s[0], s[1]);
      default: return nir_fmul(b, s[0], s[1]);
      }
   }
   if (IS("length")) {
      NEED(1);
      nir_def *d = x->num_components == 1 ? nir_fabs(b, x) : air_narrow(b, nir_fsqrt(b, air_widen(b, nir_fdot(b, x, x))), x->bit_size);
      return d;
   }
   if (IS("distance")) {
      NEED(2);
      nir_def *v = nir_fsub(b, s[0], s[1]);
      return v->num_components == 1 ? nir_fabs(b, v) : air_narrow(b, nir_fsqrt(b, air_widen(b, nir_fdot(b, v, v))), x->bit_size);
   }
   if (IS("normalize")) { NEED(1); return air_narrow(b, nir_normalize(b, air_widen(b, x)), x->bit_size); }
   if (IS("cross")) { NEED(2); return nir_cross3(b, s[0], s[1]); }
   if (IS("isnan")) { NEED(1); return air_fisnan(b, x); }
   if (IS("isinf")) {
      NEED(1);
      const uint32_t save = b->fp_math_ctrl;
      b->fp_math_ctrl |= nir_fp_preserve_nan | nir_fp_preserve_inf;
      nir_def *r = nir_feq(b, nir_fabs(b, x), nir_imm_floatN_t(b, INFINITY, x->bit_size));
      b->fp_math_ctrl = save;
      return r;
   }
   if (IS("isfinite")) {
      NEED(1);
      const uint32_t save = b->fp_math_ctrl;
      b->fp_math_ctrl |= nir_fp_preserve_nan | nir_fp_preserve_inf;
      nir_def *r = nir_fisfinite(b, x);
      b->fp_math_ctrl = save;
      return r;
   }
   if (IS("isnormal")) {
      /* NAK does not ask NIR to lower fisnormal: the exponent is neither 0 nor all ones */
      NEED(1);
      if (x->bit_size == 16)
         return nir_ult(b, nir_imm_intN_t(b, 0xfff, 16), nir_iadd_imm(b, nir_ishl_imm(b, x, 1), 0x800));
      return nir_ult(b, nir_imm_int(b, 0x1ffffff), nir_iadd_imm(b, nir_ishl_imm(b, x, 1), 0x1000000));
   }
   if (IS("signbit")) { NEED(1); return nir_ilt_imm(b, x, 0); }
   (void)rt;
   return NULL;
#undef NEED
#undef IS
}

static nir_def *
air_integer_function(struct air_tr *t, const char *fn, bool is_signed, nir_def **s, unsigned n)
{
   nir_builder *b = &t->b;
   nir_def *x = n > 0 ? s[0] : NULL;
#define IS(name) (!strcmp(fn, name))
#define NEED(k) do { if (n != (k)) unsupported(t, "air.%s with %u operands", fn, n); } while (0)
   if (IS("abs")) { NEED(1); return is_signed ? nir_iabs(b, x) : x; }
   if (IS("min")) { NEED(2); return is_signed ? nir_imin(b, s[0], s[1]) : nir_umin(b, s[0], s[1]); }
   if (IS("max")) { NEED(2); return is_signed ? nir_imax(b, s[0], s[1]) : nir_umax(b, s[0], s[1]); }
   if (IS("clamp")) {
      NEED(3);
      return is_signed ? nir_imin(b, nir_imax(b, s[0], s[1]), s[2]) : nir_umin(b, nir_umax(b, s[0], s[1]), s[2]);
   }
   if (IS("mul_hi")) { NEED(2); return is_signed ? nir_imul_high(b, s[0], s[1]) : nir_umul_high(b, s[0], s[1]); }
   if (IS("add_sat")) { NEED(2); return is_signed ? nir_iadd_sat(b, s[0], s[1]) : nir_uadd_sat(b, s[0], s[1]); }
   if (IS("sub_sat")) { NEED(2); return is_signed ? nir_isub_sat(b, s[0], s[1]) : nir_usub_sat(b, s[0], s[1]); }
   /* NAK has no [u|i][r]hadd and does not ask NIR to lower them: (x & y) + ((x ^ y) >> 1), (x | y) - ((x ^ y) >> 1) */
   if (IS("hadd") || IS("rhadd")) {
      NEED(2);
      nir_def *half = nir_ixor(b, s[0], s[1]);
      half = is_signed ? nir_ishr_imm(b, half, 1) : nir_ushr_imm(b, half, 1);
      return IS("hadd") ? nir_iadd(b, nir_iand(b, s[0], s[1]), half) : nir_isub(b, nir_ior(b, s[0], s[1]), half);
   }
   if (IS("abs_diff") || IS("absdiff")) {
      NEED(2);
      nir_def *lt = is_signed ? nir_ilt(b, s[0], s[1]) : nir_ult(b, s[0], s[1]);
      return nir_bcsel(b, lt, nir_isub(b, s[1], s[0]), nir_isub(b, s[0], s[1]));
   }
   if (IS("popcount")) { NEED(1); return air_to_bits(b, nir_bit_count(b, x), x->bit_size); }
   /* air.clz and air.ctz carry LLVM's is-zero-poison flag as a second operand; Metal defines both at zero */
   if (IS("clz")) {
      if (n != 1 && n != 2)
         unsupported(t, "air.%s with %u operands", fn, n);
      nir_def *w = nir_u2u32(b, x);
      nir_def *z = nir_isub_imm(b, (int)x->bit_size - 32, nir_imm_int(b, 0));
      return air_to_bits(b, nir_iadd(b, nir_uclz(b, w), z), x->bit_size);
   }
   if (IS("ctz")) {
      if (n != 1 && n != 2)
         unsupported(t, "air.%s with %u operands", fn, n);
      nir_def *w = nir_u2u32(b, x);
      nir_def *l = nir_find_lsb(b, w);
      return air_to_bits(b, nir_bcsel(b, nir_ieq_imm(b, w, 0), nir_imm_int(b, x->bit_size), l), x->bit_size);
   }
   if (IS("reverse_bits")) {
      NEED(1);
      if (x->bit_size != 32)
         unsupported(t, "air.reverse_bits of %u bits", x->bit_size);
      return nir_bitfield_reverse(b, x);
   }
   if (IS("extract_bits")) {
      NEED(3);
      if (x->bit_size != 32)
         unsupported(t, "air.extract_bits of %u bits", x->bit_size);
      nir_def *o = nir_u2u32(b, s[1]), *c = nir_u2u32(b, s[2]);
      return is_signed ? nir_ibitfield_extract(b, x, o, c) : nir_ubitfield_extract(b, x, o, c);
   }
   if (IS("insert_bits")) {
      NEED(4);
      if (x->bit_size != 32)
         unsupported(t, "air.insert_bits of %u bits", x->bit_size);
      return nir_bitfield_insert(b, s[0], s[1], nir_u2u32(b, s[2]), nir_u2u32(b, s[3]));
   }
   if (IS("rotate")) {
      NEED(2);
      nir_def *r = nir_u2u32(b, nir_iand_imm(b, s[1], x->bit_size - 1));
      nir_def *l = nir_ishl(b, x, r);
      nir_def *h = nir_ushr(b, x, nir_iand_imm(b, nir_isub_imm(b, x->bit_size, r), x->bit_size - 1));
      return nir_bcsel(b, nir_ieq_imm(b, r, 0), x, nir_ior(b, l, h));
   }
   if (IS("mad24") || IS("mul24")) {
      nir_def *m = nir_imul(b, s[0], s[1]);
      return IS("mad24") && n == 3 ? nir_iadd(b, m, s[2]) : m;
   }
   if (IS("mad_hi")) {
      NEED(3);
      return nir_iadd(b, is_signed ? nir_imul_high(b, s[0], s[1]) : nir_umul_high(b, s[0], s[1]), s[2]);
   }
   return NULL;
#undef NEED
#undef IS
}

/* air.pack.<format>.<type> and air.unpack.<format>.<type>: the GLSL.std.450 family the translator lowers them to
 * (dispatch_texture.rs packed_format): unorm4x8, snorm4x8, unorm2x16, snorm2x16, half2x16, NIR's ops for what
 * spirv_to_nir makes of those; a half vector is packed from float and unpacked to float, then narrowed. The other
 * formats (srgb, rgb10a2, rg11b10f, rgb9e5, rgb565) stay refused. */
static nir_def *
air_pack(struct air_tr *t, const char *name, nir_def *x, const struct air_type *rt)
{
   nir_builder *b = &t->b;
   static const struct { const char *format; nir_op pack, unpack; unsigned n; } formats[] = {
      { "unorm4x8.", nir_op_pack_unorm_4x8, nir_op_unpack_unorm_4x8, 4 },
      { "snorm4x8.", nir_op_pack_snorm_4x8, nir_op_unpack_snorm_4x8, 4 },
      { "unorm2x16.", nir_op_pack_unorm_2x16, nir_op_unpack_unorm_2x16, 2 },
      { "snorm2x16.", nir_op_pack_snorm_2x16, nir_op_unpack_snorm_2x16, 2 },
      { "half2x16.", nir_op_pack_half_2x16, nir_op_unpack_half_2x16, 2 },
   };
   const bool pack = air_starts(name, "air.pack.");
   const char *f = name + (pack ? strlen("air.pack.") : strlen("air.unpack."));
   for (unsigned i = 0; i < ARRAY_SIZE(formats); i++) {
      if (!air_starts(f, formats[i].format))
         continue;
      if (pack) {
         if (x->num_components != formats[i].n || x->bit_size == 64 || rt->is_float || rt->bits != 32 || rt->components != 1)
            break;
         return nir_build_alu1(b, formats[i].pack, x->bit_size == 32 ? x : nir_f2f32(b, x));
      }
      if (x->num_components != 1 || x->bit_size != 32 || !rt->is_float || rt->components != formats[i].n ||
          (rt->bits != 32 && rt->bits != 16))
         break;
      nir_def *r = nir_build_alu1(b, formats[i].unpack, x);
      return rt->bits == 32 ? r : nir_f2f16(b, r);
   }
   unsupported(t, "the AIR function %s", name);
}

/* air.convert.<to>.<from>: each "f.<type>", "s.<type>" or "u.<type>". */
static nir_def *
air_convert(struct air_tr *t, const char *rest, nir_def *x, const struct air_type *to)
{
   nir_builder *b = &t->b;
   char kto = rest[0];
   const char *from = strchr(rest, '.');
   from = from ? strchr(from + 1, '.') : NULL;
   if (!from || (kto != 'f' && kto != 's' && kto != 'u'))
      unsupported(t, "air.convert.%s", rest);
   char kfrom = from[1];
   unsigned bits = to->bits;
   if (bits == 1)
      return kfrom == 'f' ? nir_fneu_imm(b, x, 0.0) : nir_ine_imm(b, x, 0);
   if (x->bit_size == 1)
      return kto == 'f' ? nir_b2fN(b, x, bits) : nir_b2iN(b, x, bits);
   if (kto == 'f') {
      if (kfrom == 'f')
         return bits < x->bit_size && bits == 16 ? air_f2f16(b, x) : nir_f2fN(b, x, bits);
      return kfrom == 's' ? nir_i2fN(b, x, bits) : nir_u2fN(b, x, bits);
   }
   if (kfrom == 'f')
      return air_f2int(b, x, bits, kto == 's');
   return kfrom == 's' ? nir_i2iN(b, x, bits) : nir_u2uN(b, x, bits);
}

static void
air_barrier(struct air_tr *t, uint64_t flags, mesa_scope exec, mesa_scope scope)
{
   nir_variable_mode modes = 0;
   if (flags & 1)
      modes |= nir_var_mem_global;
   if (flags & 2)
      modes |= nir_var_mem_shared;
   if (flags & 4)
      modes |= nir_var_image;
   nir_barrier(&t->b, .execution_scope = exec, .memory_scope = modes ? scope : SCOPE_NONE,
               .memory_semantics = modes ? NIR_MEMORY_ACQ_REL : 0, .memory_modes = modes);
}

static mesa_scope
air_scope(uint64_t s)
{
   switch (s) {
   case 2: return SCOPE_DEVICE;
   case 4: return SCOPE_SUBGROUP;
   default: return SCOPE_WORKGROUP;
   }
}

/* air.atomic.<global|local>.<op>[.<s|u>].<type>(pointer, ...) */
static nir_def *
air_atomic(struct air_tr *t, const char *rest, LLVMValueRef call, unsigned nargs, bool *void_result)
{
   nir_builder *b = &t->b;
   bool global = air_starts(rest, "global.");
   if (!global && !air_starts(rest, "local."))
      unsupported(t, "air.atomic.%s", rest);
   const char *op = strchr(rest, '.') + 1;
   struct air_val *ptr = air_get(t, L.LLVMGetOperand(call, 0));
   if ((global && ptr->kind != AIR_GLOBAL) || (!global && ptr->kind != AIR_SHARED))
      unsupported(t, "an atomic on %s memory through another kind of pointer", global ? "device" : "threadgroup");
   *void_result = false;

   bool is_signed = strstr(op, ".s.") != NULL;
   bool is_float = strstr(op, ".f32") != NULL || strstr(op, ".f16") != NULL;
   nir_atomic_op aop;
   if (air_starts(op, "load.")) {
      /* an atomic add of zero reads what every other atomic has written */
      LLVMTypeRef rty = L.LLVMTypeOf(call);
      struct air_type ty = air_type_of(t, rty);
      nir_def *zero = nir_imm_intN_t(b, 0, ty.bits);
      return global ? nir_global_atomic(b, ty.bits, ptr->def, zero, .atomic_op = nir_atomic_op_iadd)
                    : nir_shared_atomic(b, ty.bits, ptr->def, zero, .atomic_op = nir_atomic_op_iadd);
   }
   if (nargs < 2)
      unsupported(t, "air.atomic.%s with %u operands", rest, nargs);
   nir_def *value = air_def(t, L.LLVMGetOperand(call, 1));
   if (air_starts(op, "cmpxchg.")) {
      struct air_val *expected = air_get(t, L.LLVMGetOperand(call, 1));
      nir_def *desired = air_def(t, L.LLVMGetOperand(call, 2));
      nir_def *cmp = air_load_mem(t, expected, 1, desired->bit_size, desired->bit_size / 8);
      nir_def *old = global ? nir_global_atomic_swap(b, desired->bit_size, ptr->def, cmp, desired,
                                                     .atomic_op = is_float ? nir_atomic_op_fcmpxchg : nir_atomic_op_cmpxchg)
                            : nir_shared_atomic_swap(b, desired->bit_size, ptr->def, cmp, desired,
                                                     .atomic_op = is_float ? nir_atomic_op_fcmpxchg : nir_atomic_op_cmpxchg);
      air_store_mem(t, expected, old, desired->bit_size / 8);
      return old;
   }
   if (air_starts(op, "store.")) {
      aop = nir_atomic_op_xchg;
      *void_result = true;
   } else if (air_starts(op, "xchg.")) aop = nir_atomic_op_xchg;
   else if (air_starts(op, "add.")) aop = is_float ? nir_atomic_op_fadd : nir_atomic_op_iadd;
   else if (air_starts(op, "sub.")) {
      if (is_float)
         value = nir_fneg(b, value), aop = nir_atomic_op_fadd;
      else
         value = nir_ineg(b, value), aop = nir_atomic_op_iadd;
   }
   else if (air_starts(op, "min.")) aop = is_float ? nir_atomic_op_fmin : is_signed ? nir_atomic_op_imin : nir_atomic_op_umin;
   else if (air_starts(op, "max.")) aop = is_float ? nir_atomic_op_fmax : is_signed ? nir_atomic_op_imax : nir_atomic_op_umax;
   else if (air_starts(op, "and.")) aop = nir_atomic_op_iand;
   else if (air_starts(op, "or.")) aop = nir_atomic_op_ior;
   else if (air_starts(op, "xor.")) aop = nir_atomic_op_ixor;
   else unsupported(t, "air.atomic.%s", rest);
   return global ? nir_global_atomic(b, value->bit_size, ptr->def, value, .atomic_op = aop)
                 : nir_shared_atomic(b, value->bit_size, ptr->def, value, .atomic_op = aop);
}

static nir_def *
air_llvm_intrinsic(struct air_tr *t, const char *name, LLVMValueRef call, nir_def **s, unsigned n, bool *handled)
{
   nir_builder *b = &t->b;
   *handled = true;
#define NAMED(p) air_starts(name, "llvm." p)
   if (NAMED("lifetime.") || NAMED("dbg.") || NAMED("assume") || NAMED("experimental.noalias.scope.decl") ||
       NAMED("invariant.") || NAMED("donothing") || NAMED("sideeffect"))
      return NULL;
   if (NAMED("memcpy.") || NAMED("memmove.") || NAMED("memcpy.inline.")) {
      air_mem_copy(t, call, false);
      return NULL;
   }
   if (NAMED("memset.")) {
      air_mem_copy(t, call, true);
      return NULL;
   }
   if (NAMED("fabs.")) return nir_fabs(b, s[0]);
   if (NAMED("sqrt.")) return air_f32_unary(b, nir_op_fsqrt, s[0]);
   if (NAMED("exp2.")) return air_f32_unary(b, nir_op_fexp2, s[0]);
   if (NAMED("log2.")) return air_f32_unary(b, nir_op_flog2, s[0]);
   if (NAMED("sin.")) return air_f32_unary(b, nir_op_fsin, s[0]);
   if (NAMED("cos.")) return air_f32_unary(b, nir_op_fcos, s[0]);
   if (NAMED("floor.")) return nir_ffloor(b, s[0]);
   if (NAMED("ceil.")) return nir_fceil(b, s[0]);
   if (NAMED("trunc.")) return nir_ftrunc(b, s[0]);
   if (NAMED("rint.") || NAMED("nearbyint.") || NAMED("roundeven.")) return nir_fround_even(b, s[0]);
   if (NAMED("round.")) return nir_fmul(b, nir_fsign(b, s[0]), nir_ffloor(b, nir_fadd_imm(b, nir_fabs(b, s[0]), 0.5)));
   if (NAMED("fma.") || NAMED("fmuladd.")) return nir_ffma(b, s[0], s[1], s[2]);
   if (NAMED("minnum.") || NAMED("minimum.")) return air_fminmax(b, false, false, s[0], s[1]);
   if (NAMED("maxnum.") || NAMED("maximum.")) return air_fminmax(b, false, true, s[0], s[1]);
   if (NAMED("pow.")) return air_narrow(b, nir_fpow(b, air_widen(b, s[0]), air_widen(b, s[1])), s[0]->bit_size);
   if (NAMED("copysign.")) {
      uint64_t sign = 1ull << (s[0]->bit_size - 1);
      return nir_ior(b, nir_iand_imm(b, s[0], ~sign & BITFIELD64_MASK(s[0]->bit_size)), nir_iand_imm(b, s[1], sign));
   }
   if (NAMED("smin.")) return nir_imin(b, s[0], s[1]);
   if (NAMED("smax.")) return nir_imax(b, s[0], s[1]);
   if (NAMED("umin.")) return nir_umin(b, s[0], s[1]);
   if (NAMED("umax.")) return nir_umax(b, s[0], s[1]);
   if (NAMED("abs.")) return nir_iabs(b, s[0]);
   if (NAMED("ctpop.")) return air_to_bits(b, nir_bit_count(b, s[0]), s[0]->bit_size);
   if (NAMED("uadd.sat.")) return nir_uadd_sat(b, s[0], s[1]);
   if (NAMED("usub.sat.")) return nir_usub_sat(b, s[0], s[1]);
   if (NAMED("sadd.sat.")) return nir_iadd_sat(b, s[0], s[1]);
   if (NAMED("ssub.sat.")) return nir_isub_sat(b, s[0], s[1]);
   if (NAMED("bitreverse.") && s[0]->bit_size == 32) return nir_bitfield_reverse(b, s[0]);
   if (NAMED("ctlz.")) return air_integer_function(t, "clz", false, s, 1);
   if (NAMED("cttz.")) return air_integer_function(t, "ctz", false, s, 1);
   if (NAMED("fshl.") || NAMED("fshr.")) {
      unsigned w = s[0]->bit_size;
      nir_def *r = nir_u2u32(b, nir_iand_imm(b, s[2], w - 1));
      nir_def *inv = nir_u2u32(b, nir_iand_imm(b, nir_isub_imm(b, w, nir_u2uN(b, r, w)), w - 1));
      nir_def *v = NAMED("fshl.") ? nir_ior(b, nir_ishl(b, s[0], r), nir_ushr(b, s[1], inv))
                                  : nir_ior(b, nir_ushr(b, s[1], r), nir_ishl(b, s[0], inv));
      return nir_bcsel(b, nir_ieq_imm(b, r, 0), NAMED("fshl.") ? s[0] : s[1], v);
   }
#undef NAMED
   *handled = false;
   return NULL;
}

/* A SIMD-group (or quad) operation on one 32-bit or 64-bit scalar: narrower integers and halves are widened first. */
static nir_def *
air_simd_scalar(struct air_tr *t, const char *fn, bool quad, bool is_float, bool is_signed, nir_def *x, nir_def *arg)
{
   nir_builder *b = &t->b;
   const unsigned width = quad ? 4 : 32;
   const unsigned bits = x->bit_size;
   nir_def *w = x;
   if (bits == 1)
      w = nir_b2i32(b, x);
   else if (bits < 32)
      w = is_float ? nir_f2f32(b, x) : is_signed ? nir_i2i32(b, x) : nir_u2u32(b, x);
#define IS(name) (!strcmp(fn, name))
   nir_op op = nir_num_opcodes;
   const char *red = fn;
   bool inclusive = false, exclusive = false;
   if (air_starts(fn, "prefix_inclusive_")) {
      inclusive = true;
      red = fn + strlen("prefix_inclusive_");
   } else if (air_starts(fn, "prefix_exclusive_")) {
      exclusive = true;
      red = fn + strlen("prefix_exclusive_");
   }
   if (!strcmp(red, "sum")) op = is_float ? nir_op_fadd : nir_op_iadd;
   else if (!strcmp(red, "product")) op = is_float ? nir_op_fmul : nir_op_imul;
   else if (!strcmp(red, "min")) op = is_float ? nir_op_fmin : is_signed ? nir_op_imin : nir_op_umin;
   else if (!strcmp(red, "max")) op = is_float ? nir_op_fmax : is_signed ? nir_op_imax : nir_op_umax;
   else if (!strcmp(red, "and") && !is_float) op = nir_op_iand;
   else if (!strcmp(red, "or") && !is_float) op = nir_op_ior;
   else if (!strcmp(red, "xor") && !is_float) op = nir_op_ixor;

   nir_def *r = NULL;
   if (op != nir_num_opcodes) {
      if ((inclusive || exclusive) && quad)
         unsupported(t, "air.quad_%s", fn);
      if (inclusive)
         r = nir_inclusive_scan(b, w, .reduction_op = op);
      else if (exclusive)
         r = nir_exclusive_scan(b, w, .reduction_op = op);
      else
         r = nir_reduce(b, w, .reduction_op = op, .cluster_size = quad ? 4 : 0);
   } else {
      /* shuffles: the lane to read, and whether a lane that falls outside the group keeps its own value */
      nir_def *lane = nir_load_subgroup_invocation(b);
      nir_def *base = quad ? nir_iand_imm(b, lane, ~3u) : nir_imm_int(b, 0);
      nir_def *in_group = quad ? nir_iand_imm(b, lane, 3) : lane;
      nir_def *a = arg ? nir_u2u32(b, arg) : NULL;
      nir_def *src = NULL, *keep = NULL;
      if (IS("shuffle") || IS("broadcast")) {
         src = nir_iand_imm(b, a, width - 1);
      } else if (IS("shuffle_xor")) {
         src = nir_ixor(b, in_group, nir_iand_imm(b, a, width - 1));
      } else if (IS("shuffle_down")) {
         src = nir_iadd(b, in_group, a);
         keep = nir_uge_imm(b, src, width);
      } else if (IS("shuffle_up")) {
         src = nir_isub(b, in_group, a);
         keep = nir_ult(b, in_group, a);
      } else if (IS("shuffle_rotate_down")) {
         src = nir_iand_imm(b, nir_iadd(b, in_group, a), width - 1);
      } else if (IS("shuffle_rotate_up")) {
         src = nir_iand_imm(b, nir_isub(b, in_group, a), width - 1);
      } else if (IS("broadcast_first") && !quad) {
         r = nir_read_first_invocation(b, w);
      } else {
         unsupported(t, "air.%s_%s", quad ? "quad" : "simd", fn);
      }
      if (!r) {
         r = nir_shuffle(b, w, nir_iadd(b, base, nir_iand_imm(b, src, width - 1)));
         if (keep)
            r = nir_bcsel(b, keep, w, r);
      }
   }
#undef IS
   if (bits == 1)
      return nir_ine_imm(b, r, 0);
   if (bits < 32)
      return is_float ? nir_f2fN(b, r, bits) : nir_u2uN(b, r, bits);
   return r;
}

/* air.simd_<fn>[.<s|u>].<type>(value[, lane]) and air.quad_<fn>...: Metal's SIMD-group functions, on NVIDIA's warp of 32
 * (thread_execution_width). Vectors go component by component. */
static nir_def *
air_simd(struct air_tr *t, const char *name, LLVMValueRef call, nir_def **s, unsigned n, const struct air_type *rt)
{
   nir_builder *b = &t->b;
   bool quad = air_starts(name, "air.quad_");
   const char *p = name + strlen("air.simd_");
   char fn[48];
   const char *dot = strchr(p, '.');
   size_t len = dot ? (size_t)(dot - p) : strlen(p);
   if (len >= sizeof fn)
      unsupported(t, "the AIR function %s", name);
   memcpy(fn, p, len);
   fn[len] = 0;
   bool is_signed = dot && air_starts(dot, ".s.");
   bool is_float = n > 0 && L.LLVMGetTypeKind(L.LLVMTypeOf(L.LLVMGetOperand(call, 0))) != TY_POINTER &&
                   air_type_of(t, L.LLVMTypeOf(L.LLVMGetOperand(call, 0))).is_float;

   if (!strcmp(fn, "ballot") || !strcmp(fn, "active_threads_mask")) {
      if (quad)
         unsupported(t, "%s", name);
      nir_def *c = n ? s[0] : nir_imm_true(b);
      if (c->bit_size != 1 || c->num_components != 1)
         unsupported(t, "%s of a %u-bit value", name, c->bit_size);
      nir_def *m = nir_ballot(b, 1, 32, c);
      return rt->bits == 32 ? m : nir_u2uN(b, m, rt->bits);
   }
   if (!strcmp(fn, "all") || !strcmp(fn, "any")) {
      if (quad || n != 1 || s[0]->bit_size != 1 || s[0]->num_components != 1)
         unsupported(t, "%s", name);
      return fn[1] == 'l' ? nir_vote_all(b, 1, s[0]) : nir_vote_any(b, 1, s[0]);
   }
   if (!strcmp(fn, "is_first") && !quad)
      return nir_elect(b, 1);
   if (n < 1 || n > 2)
      unsupported(t, "%s with %u operands", name, n);
   nir_def *x = s[0];
   if (x->bit_size > 32 && !strcmp(fn, "sum") && is_float)
      unsupported(t, "%s of doubles", name);
   nir_def *comps[NIR_MAX_VEC_COMPONENTS];
   for (unsigned i = 0; i < x->num_components; i++)
      comps[i] = air_simd_scalar(t, fn, quad, is_float, is_signed, nir_channel(b, x, i), n > 1 ? s[1] : NULL);
   return nir_vec(b, comps, x->num_components);
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Textures and samplers                                                                                              */
/*                                                                                                                    */
/* The bindings are the translator's (nvmtl_air_nir_abi.h): a texture read or sampled is a sampled image at 32 + n    */
/* read with txf/txl/txd/tg4 (a texture_buffer a uniform texel buffer), a texture written or read-written a storage   */
/* image without a format at 480 + n, an argument sampler 160 + n, a constexpr sampler the slot the translator's      */
/* reflection gives it.                                                                                               */

struct air_texture {
   unsigned index;               /* [[texture(n)]] */
   enum glsl_sampler_dim dim;
   bool array, storage, depth, absent;
   enum glsl_base_type base;     /* float, int or uint: what a read gives before narrowing */
   enum gl_access_qualifier access;
   nir_variable *var;
   const char *type_name;
   nir_def *heap_slot;           /* an array<texture, N> element chosen at run time: its slot in the bindless heap
                                  * (var is the heap seen as an array of this shape), read non-uniform */
};

/* An array<texture, N> argument at [[texture(n)]] (version 6): element k is the texture at index n + k. One the index
 * of which is known is that texture's own binding, as a [[texture(n + k)]] is; one chosen at run time is read as the
 * translator reads every element (resources/texture_array.rs route_texture_array_elements_through_heap): the slot in
 * the per-draw table at entry 64 + n + min(i, N - 1), its low 32 bits masked to the heap's 131072 slots, into the
 * bindless heap (set 2, binding 0), non-uniform; with NVMTL_AIR_NIR_HEAP only, and only for sampled textures (the
 * translator's storage arrays are descriptor arrays the plugin's count-1 bindings do not hold). */
struct air_texarray {
   unsigned base, count;
   struct air_arg arg;           /* the argument, with the element's type name */
   unsigned param;               /* its parameter's index, for messages */
   struct air_texture proto;     /* an element's shape */
   struct air_texture **elems;   /* by known index, made when first read */
   nir_variable *heap;
   bool absent;
};

struct air_sampler {
   unsigned slot;                /* binding 160 + slot */
   bool is_static, read_sampler, absent;
   bool given;                   /* an argument sampler whose state the request gives (version 5) */
   uint64_t words[2];            /* a constexpr sampler's AIR state, or the state the request gives */
   nir_variable *var, *shadow_var;
   LLVMValueRef global;
};

/* texture2d<float, read>, texture2d_array<half, sample>, depth2d<float>, texture_buffer<uint, read_write>, ... */
static void
air_texture_shape(struct air_tr *t, struct air_texture *x, const char *type_name)
{
   static const struct { const char *name; enum glsl_sampler_dim dim; bool array, depth; } shapes[] = {
      { "texture1d_array", GLSL_SAMPLER_DIM_1D, true, false },   { "texture1d", GLSL_SAMPLER_DIM_1D, false, false },
      { "texture2d_ms_array", GLSL_SAMPLER_DIM_MS, true, false }, { "texture2d_ms", GLSL_SAMPLER_DIM_MS, false, false },
      { "texture2d_array", GLSL_SAMPLER_DIM_2D, true, false },   { "texture2d", GLSL_SAMPLER_DIM_2D, false, false },
      { "texture3d", GLSL_SAMPLER_DIM_3D, false, false },
      { "texturecube_array", GLSL_SAMPLER_DIM_CUBE, true, false }, { "texturecube", GLSL_SAMPLER_DIM_CUBE, false, false },
      { "texture_buffer", GLSL_SAMPLER_DIM_BUF, false, false },
      { "depth2d_ms_array", GLSL_SAMPLER_DIM_MS, true, true },   { "depth2d_ms", GLSL_SAMPLER_DIM_MS, false, true },
      { "depth2d_array", GLSL_SAMPLER_DIM_2D, true, true },      { "depth2d", GLSL_SAMPLER_DIM_2D, false, true },
      { "depthcube_array", GLSL_SAMPLER_DIM_CUBE, true, true },  { "depthcube", GLSL_SAMPLER_DIM_CUBE, false, true },
   };
   if (!type_name)
      unsupported(t, "texture argument %u has no air.arg_type_name", x->index);
   for (unsigned i = 0; i < ARRAY_SIZE(shapes); i++) {
      size_t n = strlen(shapes[i].name);
      if (!strncmp(type_name, shapes[i].name, n) && (type_name[n] == '<' || type_name[n] == 0)) {
         x->dim = shapes[i].dim;
         x->array = shapes[i].array;
         x->depth = shapes[i].depth;
         const char *c = type_name[n] == '<' ? type_name + n + 1 : "float";
         if (!strncmp(c, "uint", 4) || !strncmp(c, "ushort", 6) || !strncmp(c, "uchar", 5))
            x->base = GLSL_TYPE_UINT;
         else if (!strncmp(c, "int", 3) || !strncmp(c, "short", 5) || !strncmp(c, "char", 4))
            x->base = GLSL_TYPE_INT;
         else if (!strncmp(c, "float", 5) || !strncmp(c, "half", 4))
            x->base = GLSL_TYPE_FLOAT;
         else
            unsupported(t, "a texture of %s", type_name);
         return;
      }
   }
   unsupported(t, "the texture type %s", type_name);
}

static void
air_mask_set(uint32_t *mask, unsigned bit)
{
   mask[bit / 32] |= 1u << (bit % 32);
}

static struct air_texture *
air_bind_texture(struct air_tr *t, const struct air_arg *a, unsigned i)
{
   struct air_texture *x = rzalloc(t->mem, struct air_texture);
   if (air_fc_arg_present(t, a) == 0) {
      /* a function constant leaves it out: nothing is bound, and a use is refused */
      x->absent = true;
      x->index = (unsigned)MIN2(a->location, 127);
      return x;
   }
   if (a->location >= 128)
      unsupported(t, "texture argument %u has no texture index below 128", i);
   if (L.LLVMGetTypeKind(L.LLVMTypeOf(a->param)) != TY_POINTER)
      unsupported(t, "texture argument %u is not a handle", i);
   x->index = (unsigned)a->location;
   x->type_name = a->type_name;
   air_texture_shape(t, x, a->type_name);
   const char *access = a->access ? a->access : "air.sample";
   x->storage = !strcmp(access, "air.write") || !strcmp(access, "air.read_write");
   if (!strcmp(access, "air.write"))
      x->access = ACCESS_NON_READABLE;
   /* a multisampled texture is read by sample (txf_ms) and queried; a depth one, or one written, stays refused */
   if (x->dim == GLSL_SAMPLER_DIM_MS && (x->depth || x->storage))
      unsupported(t, "the multisampled texture %s", a->type_name);
   if (x->storage && (x->dim == GLSL_SAMPLER_DIM_CUBE || x->depth))
      unsupported(t, "a written %s (the translator writes it through a 2D-array view)", a->type_name);
   const struct glsl_type *type = x->storage ? glsl_image_type(x->dim, x->array, x->base)
                                             : glsl_texture_type(x->dim, x->array, x->base);
   x->var = nir_variable_create(t->nir, x->storage ? nir_var_image : nir_var_uniform, type, a->type_name);
   x->var->data.descriptor_set = t->set;
   x->var->data.binding = (x->storage ? 480 : 32) + x->index;
   if (x->storage) {
      x->var->data.access = x->access;
      x->var->data.image.format = PIPE_FORMAT_NONE;
   }
   return x;
}

/* the element type and the length of "array<texture2d<float, sample>, 4>" (meta::texture_handle_array); false for
 * anything else, an array_ref<...> among them */
static bool
air_texarray_type(struct air_tr *t, const char *name, char **elem, unsigned *count)
{
   if (!name || strncmp(name, "array<", 6) || (strncmp(name + 6, "texture", 7) && strncmp(name + 6, "depth", 5)))
      return false;
   const char *end = strrchr(name, '>'), *comma = NULL;
   for (const char *p = name; end && p < end; p++)
      if (*p == ',')
         comma = p;
   if (!end || !comma)
      return false;
   char *e = ralloc_strndup(t->mem, name + 6, comma - (name + 6));
   unsigned long n = strtoul(comma + 1, NULL, 10);
   if (!n || n > 128)
      return false;
   *elem = e;
   *count = (unsigned)n;
   return true;
}

static struct air_texarray *
air_bind_texarray(struct air_tr *t, const struct air_arg *a, unsigned i, const char *elem, unsigned count)
{
   struct air_texarray *x = rzalloc(t->mem, struct air_texarray);
   x->arg = *a;
   x->arg.type_name = elem;
   x->arg.fc_node = NULL;
   x->param = i;
   x->count = count;
   x->base = (unsigned)MIN2(a->location, 127);
   const int present = air_fc_arg_present(t, a);
   if (present < 0)
      unsupported(t, "texture argument %u exists only under a function constant %s", i,
                  air_has_fc_values(t) ? "the request does not give" : "(the request has no function-constant values)");
   x->absent = present == 0;
   if (x->absent)
      return x;
   if (a->location + count > 128)
      unsupported(t, "texture argument %u, an array of %u from index %llu, has no texture indices below 128", i, count,
                  (unsigned long long)a->location);
   if (L.LLVMGetTypeKind(L.LLVMTypeOf(a->param)) != TY_POINTER)
      unsupported(t, "texture argument %u, an array of textures, is not passed by its address", i);
   x->proto.index = x->base;
   x->proto.type_name = elem;
   air_texture_shape(t, &x->proto, elem);
   const char *access = a->access ? a->access : "air.sample";
   x->proto.storage = !strcmp(access, "air.write") || !strcmp(access, "air.read_write");
   x->elems = rzalloc_array(t->mem, struct air_texture *, count);
   return x;
}

/* a handle loaded from an array<texture, N> argument: element ptr->def */
static struct air_val *
air_texarray_load(struct air_tr *t, struct air_val *ptr)
{
   nir_builder *b = &t->b;
   struct air_texarray *x = ptr->res;
   struct air_val *v = air_make(t, NULL, AIR_TEXTURE, 1);
   if (x->absent) {
      struct air_texture *a = rzalloc(t->mem, struct air_texture);
      a->absent = true;
      a->index = x->base;
      v->res = a;
      return v;
   }
   if (nir_src_is_const(nir_src_for_ssa(ptr->def))) {
      /* the translator clamps an index past the array to its last element */
      const unsigned k = (unsigned)MIN2(nir_src_as_uint(nir_src_for_ssa(ptr->def)), (uint64_t)x->count - 1);
      if (!x->elems[k]) {
         struct air_arg e = x->arg;
         e.location = x->base + k;
         x->elems[k] = air_bind_texture(t, &e, x->param);
      }
      v->res = x->elems[k];
      return v;
   }
   if (x->proto.storage)
      unsupported(t, "an element of a written array of textures chosen at run time (the translator's is a descriptor array "
                  "the plugin's bindings do not hold)");
   if (x->proto.dim == GLSL_SAMPLER_DIM_BUF)
      unsupported(t, "an element of an array of texture_buffers chosen at run time");
   if (!(t->req->flags & NVMTL_AIR_NIR_HEAP))
      unsupported(t, "an element of an array of textures chosen at run time, without the bindless heap (NVMTL_AIR_NIR_HEAP)");
   if (!x->heap) {
      const struct glsl_type *et = glsl_texture_type(x->proto.dim, x->proto.array, x->proto.base);
      x->heap = nir_variable_create(t->nir, nir_var_uniform, glsl_array_type(et, 0, 0), "bindless_heap");
      x->heap->data.descriptor_set = NVMTL_AIR_NIR_HEAP_SET;
      x->heap->data.binding = NVMTL_AIR_NIR_HEAP_BINDING;
   }
   nir_def *i = nir_umin(b, nir_u2u32(b, ptr->def), nir_imm_int(b, (int)x->count - 1));
   nir_def *entry = nir_iadd_imm(b, i, NVMTL_AIR_NIR_TABLE_TEXTURE0 + x->base);
   air_table_desc(t);
   nir_deref_instr *d = nir_build_deref_cast(b, t->table, t->table_mode,
                                             glsl_array_type(glsl_uint64_t_type(), NVMTL_AIR_NIR_TABLE_ENTRIES, 8), 0);
   d->cast.align_mul = 16;
   d->cast.align_offset = 0;
   nir_def *word = nir_load_deref_with_access(b, nir_build_deref_array(b, d, entry), ACCESS_NON_WRITEABLE | ACCESS_CAN_REORDER);
   struct air_texture *e = rzalloc(t->mem, struct air_texture);
   *e = x->proto;
   e->var = x->heap;
   e->heap_slot = nir_iand_imm(b, nir_u2u32(b, word), NVMTL_AIR_NIR_HEAP_SLOTS - 1);
   t->heap_textures = true;
   v->res = e;
   return v;
}

static struct air_sampler *
air_bind_sampler(struct air_tr *t, const struct air_arg *a, unsigned i)
{
   struct air_sampler *s = rzalloc(t->mem, struct air_sampler);
   s->absent = air_fc_arg_present(t, a) == 0;
   if (s->absent)
      return s;
   if (a->location >= 16)
      unsupported(t, "sampler argument %u has no sampler index below 16", i);
   s->slot = (unsigned)a->location;
   t->sampler_args |= 1u << s->slot;
   /* the state the pipeline is bound with, when the request gives it (a pixel-coordinate sampler's variant) */
   if ((t->req->flags & NVMTL_AIR_NIR_SAMPLER_STATES) &&
       t->req->size >= offsetof(struct nvmtl_air_nir_request, sampler_state_words) + sizeof t->req->sampler_state_words) {
      for (uint32_t k = 0; k < t->req->sampler_state_count && t->req->sampler_state_slots && t->req->sampler_state_words; k++) {
         if (t->req->sampler_state_slots[k] != s->slot)
            continue;
         s->given = true;
         s->words[0] = t->req->sampler_state_words[2 * k];
         s->words[1] = t->req->sampler_state_words[2 * k + 1];
      }
   }
   return s;
}

/* a sampler whose state is known here (a constexpr one, or an argument one the request gives) has pixel coordinates */
static bool
air_sampler_pixel(const struct air_sampler *s)
{
   return (s->is_static || s->given) && ((s->words[0] >> 15) & 1);
}

/* The order of __air_sampler_state globals the translator's reflection takes (meta::static_sampler_name_order): the
 * numbers after the prefix, then the name. */
static int
air_sampler_name_cmp(const void *pa, const void *pb)
{
   const char *a = air_name(*(LLVMValueRef const *)pa, NULL), *b = air_name(*(LLVMValueRef const *)pb, NULL);
   const char *prefix = "__air_sampler_state";
   const char *x = strncmp(a, prefix, strlen(prefix)) ? "" : a + strlen(prefix);
   const char *y = strncmp(b, prefix, strlen(prefix)) ? "" : b + strlen(prefix);
   for (;;) {
      while (*x == '.')
         x++;
      while (*y == '.')
         y++;
      if (!*x || !*y)
         return *x ? 1 : *y ? -1 : strcmp(a, b);
      char *ex, *ey;
      unsigned long long u = strtoull(x, &ex, 10), v = strtoull(y, &ey, 10);
      if (ex == x)
         u = ~0ull;
      if (ey == y)
         v = ~0ull;
      if (u != v)
         return u < v ? -1 : 1;
      x = ex == x ? x + strcspn(x, ".") : ex;
      y = ey == y ? y + strcspn(y, ".") : ey;
   }
}

/* Every constexpr sampler of !air.sampler_states takes the first sampler slot no [[sampler(n)]] argument takes, in the
 * translator's order, used or not, as its reflection gives them to the plugin (reflect::add_static_samplers). */
static void
air_assign_static_samplers(struct air_tr *t)
{
   unsigned n = L.LLVMGetNamedMetadataNumOperands(t->module, "air.sampler_states");
   if (!n)
      return;
   LLVMValueRef *nodes = rzalloc_array(t->mem, LLVMValueRef, n);
   L.LLVMGetNamedMetadataOperands(t->module, "air.sampler_states", nodes);
   LLVMValueRef *globals = rzalloc_array(t->mem, LLVMValueRef, n);
   unsigned count = 0;
   for (unsigned i = 0; i < n; i++) {
      LLVMValueRef *ops;
      unsigned k = air_md_operands(nodes[i], &ops, t->mem);
      LLVMValueRef g = k >= 2 ? ops[1] : NULL;
      while (g && L.LLVMIsAConstantExpr(g))
         g = L.LLVMGetOperand(g, 0);
      if (!g || !L.LLVMIsAGlobalVariable(g))
         unsupported(t, "an air.sampler_state without its global");
      globals[count++] = g;
   }
   qsort(globals, count, sizeof *globals, air_sampler_name_cmp);
   uint32_t taken = t->sampler_args;
   for (unsigned i = 0; i < count; i++) {
      unsigned slot = 0;
      while (slot < 32 && (taken & (1u << slot)))
         slot++;
      if (slot >= 32)
         unsupported(t, "more constexpr samplers than the sampler band holds");
      taken |= 1u << slot;
      struct air_sampler *s = rzalloc(t->mem, struct air_sampler);
      s->slot = slot;
      s->is_static = true;
      s->global = globals[i];
      LLVMValueRef init = L.LLVMGetInitializer(globals[i]);
      /* an older module's state is one i64, not [2 x i64]: its second word is 0, as the translator reads it (LLVM's
       * aggregate element of a scalar is not 0) */
      if (init && L.LLVMIsAConstantInt(init)) {
         s->words[0] = L.LLVMConstIntGetZExtValue(init);
         init = NULL;
      }
      for (unsigned w = 0; w < 2 && init; w++) {
         LLVMValueRef e = L.LLVMGetAggregateElement(init, w);
         if (e && L.LLVMIsAConstantInt(e))
            s->words[w] = L.LLVMConstIntGetZExtValue(e);
      }
      util_dynarray_append(&t->statics, s);
      struct air_val *v = air_make(t, NULL, AIR_SAMPLER, 1);
      v->res = s;
      _mesa_hash_table_insert(t->globals, globals[i], v);
   }
}

static nir_deref_instr *
air_sampler_deref(struct air_tr *t, struct air_sampler *s, bool shadow)
{
   nir_variable **var = shadow ? &s->shadow_var : &s->var;
   if (s->read_sampler)
      unsupported(t, "a sample through the read sampler");
   if (s->absent)
      unsupported(t, "a sampler a function constant leaves out is used");
   if (s->is_static || s->given) {
      uint64_t w = s->words[0];
      if (((w >> 9) & 3) == 2 || ((w >> 11) & 3) == 2)
         unsupported(t, "a %s sampler with bicubic filtering", s->is_static ? "constexpr" : "bound");
      if (((w >> 58) & 3) != 0)
         unsupported(t, "a %s sampler with min/max reduction", s->is_static ? "constexpr" : "bound");
   }
   if (!*var) {
      *var = nir_variable_create(t->nir, nir_var_uniform, shadow ? glsl_bare_shadow_sampler_type() : glsl_bare_sampler_type(),
                                 s->is_static ? "constexpr_sampler" : "sampler");
      (*var)->data.descriptor_set = t->set;
      (*var)->data.binding = 160 + s->slot;
   }
   t->sampler_mask |= 1u << s->slot;
   return nir_build_deref_var(&t->b, *var);
}

static nir_deref_instr *
air_texture_deref(struct air_tr *t, struct air_texture *x)
{
   if (x->absent)
      unsupported(t, "texture %u, which a function constant leaves out, is used", x->index);
   if (x->heap_slot)
      return nir_build_deref_array(&t->b, nir_build_deref_var(&t->b, x->var), x->heap_slot);
   if (x->storage) {
      air_mask_set(t->storage_mask, x->index);
      if (x->dim == GLSL_SAMPLER_DIM_BUF)
         air_mask_set(t->stexbuf_mask, x->index);
   } else {
      air_mask_set(t->texture_mask, x->index);
      if (x->dim == GLSL_SAMPLER_DIM_BUF)
         air_mask_set(t->texbuf_mask, x->index);
   }
   return nir_build_deref_var(&t->b, x->var);
}

static unsigned
air_spatial(const struct air_texture *x)
{
   switch (x->dim) {
   case GLSL_SAMPLER_DIM_1D: case GLSL_SAMPLER_DIM_BUF: return 1;
   case GLSL_SAMPLER_DIM_3D: case GLSL_SAMPLER_DIM_CUBE: return 3;
   default: return 2;
   }
}

static nir_alu_type
air_tex_type(const struct air_texture *x)
{
   return x->base == GLSL_TYPE_UINT ? nir_type_uint32 : x->base == GLSL_TYPE_INT ? nir_type_int32 : nir_type_float32;
}

struct air_tex_srcs {
   nir_def *coord, *lod, *bias, *comparator, *ddx, *ddy, *offset, *ms_index;
   nir_deref_instr *texture, *sampler;
};

static nir_def *
air_build_tex(struct air_tr *t, nir_texop op, const struct air_texture *x, const struct air_tex_srcs *in, int component)
{
   nir_builder *b = &t->b;
   nir_tex_src srcs[8];
   unsigned n = 0;
   srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_texture_deref, &in->texture->def);
   if (in->sampler)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_sampler_deref, &in->sampler->def);
   if (in->coord)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_coord, in->coord);
   if (in->lod)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_lod, in->lod);
   if (in->bias)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_bias, in->bias);
   if (in->comparator)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_comparator, in->comparator);
   if (in->ddx) {
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_ddx, in->ddx);
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_ddy, in->ddy);
   }
   if (in->offset)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_offset, in->offset);
   if (in->ms_index)
      srcs[n++] = nir_tex_src_for_ssa(nir_tex_src_ms_index, in->ms_index);
   nir_tex_instr *tex = nir_tex_instr_create(b->shader, n);
   tex->op = op;
   tex->sampler_dim = x->dim;
   tex->is_array = x->array;
   tex->texture_non_uniform = x->heap_slot != NULL;   /* the translator's heap accesses are NonUniform */
   tex->is_shadow = in->comparator != NULL;
   tex->is_new_style_shadow = in->comparator != NULL;
   const bool int_query = op == nir_texop_txs || op == nir_texop_query_levels || op == nir_texop_texture_samples;
   tex->dest_type = int_query ? nir_type_int32 : air_tex_type(x);
   if (x->depth && !int_query)
      tex->dest_type = nir_type_float32;
   tex->component = component > 0 ? (unsigned)component : 0;
   tex->coord_components = in->coord ? in->coord->num_components : 0;
   memcpy(tex->src, srcs, n * sizeof *srcs);
   nir_def_init(&tex->instr, &tex->def, nir_tex_instr_dest_size(tex), 32);
   nir_builder_instr_insert(b, &tex->instr);
   return &tex->def;
}

static bool
air_is_int(LLVMValueRef v)
{
   LLVMTypeRef ty = L.LLVMTypeOf(v);
   if (L.LLVMGetTypeKind(ty) == TY_VECTOR)
      ty = L.LLVMGetElementType(ty);
   return L.LLVMGetTypeKind(ty) == TY_INTEGER;
}

static bool
air_is_bool(LLVMValueRef v)
{
   LLVMTypeRef ty = L.LLVMTypeOf(v);
   return L.LLVMGetTypeKind(ty) == TY_INTEGER && L.LLVMGetIntTypeWidth(ty) == 1;
}

static bool
air_is_float_scalar(LLVMValueRef v)
{
   int k = L.LLVMGetTypeKind(L.LLVMTypeOf(v));
   return k == TY_FLOAT || k == TY_HALF;
}

static bool
air_is_zero(LLVMValueRef v)
{
   if (L.LLVMIsAConstantAggregateZero(v))
      return true;
   if (L.LLVMIsAConstantInt(v))
      return L.LLVMConstIntGetZExtValue(v) == 0;
   if (L.LLVMIsAConstantFP(v)) {
      int loses;
      return L.LLVMConstRealGetDouble(v, &loses) == 0.0;
   }
   return false;
}

static nir_def *
air_int32(struct air_tr *t, LLVMValueRef v)
{
   nir_def *d = air_def(t, v);
   return d->bit_size == 32 ? d : nir_u2u32(&t->b, d);
}

static nir_def *
air_append(nir_builder *b, nir_def *v, nir_def *last)
{
   nir_def *c[NIR_MAX_VEC_COMPONENTS];
   for (unsigned i = 0; i < v->num_components; i++)
      c[i] = nir_channel(b, v, i);
   c[v->num_components] = last;
   return nir_vec(b, c, v->num_components + 1);
}

static nir_def *
air_pad4(nir_builder *b, nir_def *v)
{
   nir_def *c[4];
   for (unsigned i = 0; i < 4; i++)
      c[i] = i < v->num_components ? nir_channel(b, v, i) : nir_imm_int(b, 0);
   return nir_vec(b, c, 4);
}

/* What a texture call returns: {<4 x T>, i8} (the i8, residency, is 0 here) or the color itself; the color narrowed to
 * the call's component type (half, short) and its width (a depth sample is one float). */
static struct air_val *
air_texel_result(struct air_tr *t, LLVMValueRef call, nir_def *color)
{
   nir_builder *b = &t->b;
   LLVMTypeRef rty = L.LLVMTypeOf(call);
   LLVMTypeRef cty = L.LLVMGetTypeKind(rty) == TY_STRUCT ? L.LLVMStructGetTypeAtIndex(rty, 0) : rty;
   struct air_type ct = air_type_of(t, cty);
   if (ct.components < color->num_components)
      color = nir_trim_vector(b, color, ct.components);
   else if (ct.components > color->num_components)
      unsupported(t, "a texture call giving %u components", ct.components);
   if (ct.bits != color->bit_size)
      color = ct.is_float ? nir_f2fN(b, color, ct.bits) : nir_u2uN(b, color, ct.bits);
   struct air_val *c = air_make(t, color, AIR_VALUE, 0);
   if (L.LLVMGetTypeKind(rty) != TY_STRUCT)
      return c;
   unsigned n = L.LLVMCountStructElementTypes(rty);
   struct air_val *r = air_aggregate(t, n);
   r->elems[0] = c;
   for (unsigned i = 1; i < n; i++) {
      struct air_type et = air_type_of(t, L.LLVMStructGetTypeAtIndex(rty, i));
      r->elems[i] = air_make(t, nir_imm_intN_t(&t->top, 0, et.bits), AIR_VALUE, 0);
   }
   return r;
}

static struct air_texture *
air_texture_of(struct air_tr *t, LLVMValueRef v)
{
   struct air_val *x = air_get(t, v);
   if (x->kind != AIR_TEXTURE)
      unsupported(t, "a texture call on a value that is not a texture argument");
   return x->res;
}

static struct air_sampler *
air_sampler_of(struct air_tr *t, LLVMValueRef v)
{
   struct air_val *x = air_get(t, v);
   if (x->kind != AIR_SAMPLER)
      unsupported(t, "a sample through a value that is not a sampler");
   return x->res;
}

/* The texture calls: true when name is one (and *out its result). */
static bool
air_texture_call(struct air_tr *t, const char *name, LLVMValueRef call, unsigned nargs, struct air_val **out)
{
   nir_builder *b = &t->b;
   *out = NULL;
#define ARG(i) ((i) < nargs ? L.LLVMGetOperand(call, i) : NULL)
   if (air_starts(name, "air.get_read_sampler")) {
      struct air_sampler *s = rzalloc(t->mem, struct air_sampler);
      s->read_sampler = true;
      *out = air_make(t, NULL, AIR_SAMPLER, 1);
      (*out)->res = s;
      return true;
   }
   bool read_depth = air_starts(name, "air.read_depth_");
   bool read = air_starts(name, "air.read_texture_") || read_depth, write = air_starts(name, "air.write_texture_");
   bool sample = air_starts(name, "air.sample_texture_"), depth_sample = air_starts(name, "air.sample_depth_");
   bool compare = air_starts(name, "air.sample_compare_depth_"), gather = air_starts(name, "air.gather_texture_");
   bool fence = air_starts(name, "air.fence_texture_");
   const char *query = NULL;
   static const char *const queries[] = { "air.get_width_", "air.get_height_", "air.get_depth_",
                                          "air.get_array_size_", "air.get_num_mip_levels_", "air.get_num_samples_" };
   for (unsigned i = 0; i < ARRAY_SIZE(queries); i++) {
      if (air_starts(name, queries[i]) && (strstr(name, "_texture_") || strstr(name, "_depth_")))
         query = queries[i];
   }
   if (!read && !write && !sample && !depth_sample && !compare && !gather && !fence && !query) {
      if (air_starts(name, "air.gather_depth_") ||
          air_starts(name, "air.gather_compare_") || air_starts(name, "air.sample_texture") ||
          air_starts(name, "air.get_num_samples_") || air_starts(name, "air.is_null_texture") ||
          air_starts(name, "air.get_null_texture") || strstr(name, "_texture_") || strstr(name, "texture_buffer"))
         unsupported(t, "the texture call %s", name);
      return false;
   }
   struct air_texture *x = air_texture_of(t, ARG(0));

   if (fence) {
      nir_barrier(b, .execution_scope = SCOPE_NONE, .memory_scope = SCOPE_WORKGROUP,
                  .memory_semantics = NIR_MEMORY_ACQ_REL, .memory_modes = nir_var_image);
      return true;
   }

   if (query) {
      nir_deref_instr *d = air_texture_deref(t, x);
      LLVMValueRef lodv = ARG(1);
      nir_def *lod = lodv ? air_int32(t, lodv) : nir_imm_int(b, 0);
      nir_def *r;
      unsigned comp = 0;
      if (!strcmp(query, "air.get_height_"))
         comp = 1;
      else if (!strcmp(query, "air.get_depth_"))
         comp = 2;
      else if (!strcmp(query, "air.get_array_size_"))
         comp = air_spatial(x) == 3 ? 3 : air_spatial(x);
      if (x->dim == GLSL_SAMPLER_DIM_CUBE && comp == 3)
         comp = 2;   /* txs of a cube array: width, height, cubes */
      if (!strcmp(query, "air.get_num_samples_")) {
         if (x->dim != GLSL_SAMPLER_DIM_MS)
            unsupported(t, "%s of a texture that is not multisampled", name);
         struct air_tex_srcs in = { .texture = d };
         r = air_build_tex(t, nir_texop_texture_samples, x, &in, -1);
      } else if (!strcmp(query, "air.get_num_mip_levels_")) {
         if (x->storage) {
            r = nir_imm_int(b, 1);
         } else {
            struct air_tex_srcs in = { .texture = d };
            r = air_build_tex(t, nir_texop_query_levels, x, &in, -1);
         }
      } else if (x->storage) {
         if (!air_is_zero(lodv ? lodv : NULL) && lodv)
            unsupported(t, "a size query of a storage texture at a level other than 0");
         unsigned nc = glsl_get_sampler_dim_coordinate_components(x->dim) + (x->array ? 1 : 0);
         r = nir_image_deref_size(b, nc, 32, &d->def, nir_imm_int(b, 0), .image_dim = x->dim, .image_array = x->array);
      } else {
         /* a buffer and a multisampled texture have one level: their size query takes no level */
         struct air_tex_srcs in = { .texture = d, .lod = x->dim == GLSL_SAMPLER_DIM_BUF || x->dim == GLSL_SAMPLER_DIM_MS ? NULL : lod };
         r = air_build_tex(t, nir_texop_txs, x, &in, -1);
      }
      if (r->num_components > 1) {
         if (comp >= r->num_components)
            unsupported(t, "%s of a texture without that dimension", name);
         r = nir_channel(b, r, comp);
      }
      *out = air_make(t, air_resize(b, r, air_type_of(t, L.LLVMTypeOf(call)).bits, false), AIR_VALUE, 0);
      return true;
   }

   if (write) {
      if (!x->storage)
         unsupported(t, "a write to a texture that is not access::write or read_write");
      LLVMValueRef coord = ARG(1), layer = NULL, texel, lodv;
      if (x->array) {
         layer = ARG(2);
         texel = ARG(3);
         lodv = ARG(4);
      } else {
         texel = ARG(2);
         lodv = x->dim == GLSL_SAMPLER_DIM_BUF ? NULL : ARG(3);
      }
      if (!coord || !texel)
         unsupported(t, "%s with %u operands", name, nargs);
      if (lodv && !air_is_zero(lodv))
         unsupported(t, "a write to a level other than 0 of a storage texture");
      nir_def *c = nir_trim_vector(b, air_int32(t, coord), air_spatial(x));
      if (x->array)
         c = air_append(b, c, air_int32(t, layer));
      nir_def *v = air_def(t, texel);
      if (v->num_components != 4)
         unsupported(t, "a texture write of %u components", v->num_components);
      if (x->base == GLSL_TYPE_FLOAT)
         v = v->bit_size == 32 ? v : nir_f2f32(b, v);
      else
         v = v->bit_size == 32 ? v : x->base == GLSL_TYPE_INT ? nir_i2i32(b, v) : nir_u2u32(b, v);
      nir_deref_instr *d = air_texture_deref(t, x);
      nir_image_deref_store(b, &d->def, air_pad4(b, c), nir_undef(b, 1, 32), v, nir_imm_int(b, 0),
                            .image_dim = x->dim, .image_array = x->array, .format = PIPE_FORMAT_NONE,
                            .access = x->access, .src_type = air_tex_type(x));
      return true;
   }

   if (read) {
      /* the translator's read_operands: (texture, [read sampler,] coord, [layer,] [offset,] lod, access) */
      LLVMValueRef coord, layer = NULL, lodv = NULL;
      if (read_depth) {
         /* (texture, read sampler, i32, coord, [layer,] offset, lod, access) */
         coord = ARG(3);
         layer = x->array ? ARG(4) : NULL;
         lodv = ARG(x->array ? 6 : 5);
      } else if (air_is_int(ARG(1))) {
         coord = ARG(1);
         if (x->array) {
            layer = ARG(2);
            lodv = ARG(3);
         } else {
            lodv = ARG(2);
         }
      } else if (x->array) {
         coord = ARG(2);
         layer = ARG(3);
         lodv = nargs >= 7 ? ARG(5) : ARG(4);
      } else {
         coord = ARG(2);
         lodv = nargs == 5 ? ARG(3) : ARG(4);
      }
      if (x->dim == GLSL_SAMPLER_DIM_BUF)
         lodv = NULL;
      if (x->dim == GLSL_SAMPLER_DIM_CUBE)
         unsupported(t, "a read of a texturecube (the translator reads it through a 2D-array view)");
      if (!coord)
         unsupported(t, "%s with %u operands", name, nargs);
      nir_def *c = nir_trim_vector(b, air_int32(t, coord), air_spatial(x));
      if (x->array)
         c = air_append(b, c, air_int32(t, layer));
      nir_def *color;
      nir_deref_instr *d = air_texture_deref(t, x);
      if (x->storage) {
         if (lodv && !air_is_zero(lodv))
            unsupported(t, "a read of a level other than 0 of a storage texture");
         color = nir_image_deref_load(b, 4, 32, &d->def, air_pad4(b, c), nir_undef(b, 1, 32), nir_imm_int(b, 0),
                                      .image_dim = x->dim, .image_array = x->array, .format = PIPE_FORMAT_NONE,
                                      .access = x->access, .dest_type = air_tex_type(x));
      } else if (x->dim == GLSL_SAMPLER_DIM_MS) {
         /* read_texture_2d_ms[_array](texture, read sampler, coord, [layer,] sample, access): the sample where a
          * level would be, a texel fetch of that sample (the translator's OpImageFetch with Sample) */
         if (!lodv)
            unsupported(t, "%s without its sample", name);
         struct air_tex_srcs in = { .texture = d, .coord = c, .ms_index = air_int32(t, lodv) };
         color = air_build_tex(t, nir_texop_txf_ms, x, &in, -1);
      } else {
         struct air_tex_srcs in = { .texture = d, .coord = c,
                                    .lod = x->dim == GLSL_SAMPLER_DIM_BUF ? NULL : lodv ? air_int32(t, lodv) : nir_imm_int(b, 0) };
         color = air_build_tex(t, nir_texop_txf, x, &in, -1);
      }
      *out = air_texel_result(t, call, color);
      return true;
   }

   /* sample, sample_depth, sample_compare_depth, gather: (texture, sampler, [i32,] coord, [layer,] [reference,] rest) */
   if (x->storage)
      unsupported(t, "a sample of a storage texture");
   if (x->dim == GLSL_SAMPLER_DIM_BUF)
      unsupported(t, "a sample of a texture_buffer");
   struct air_sampler *s = air_sampler_of(t, ARG(1));
   unsigned at = 2;
   if ((depth_sample || compare) && ARG(at) && air_is_int(ARG(at)) && !air_is_bool(ARG(at)) &&
       L.LLVMGetTypeKind(L.LLVMTypeOf(ARG(at))) == TY_INTEGER)
      at++;   /* sample_depth's leading i32 */
   LLVMValueRef coordv = ARG(at);
   at++;
   if (!coordv || air_is_int(coordv))
      unsupported(t, "%s without a float coordinate", name);
   nir_def *coord = air_def(t, coordv);
   if (coord->bit_size != 32)
      coord = nir_f2f32(b, coord);
   unsigned spatial = air_spatial(x);
   if (coord->num_components < spatial)
      unsupported(t, "a %u-component coordinate for a %u-dimensional texture", coord->num_components, spatial);
   coord = nir_trim_vector(b, coord, spatial);
   nir_def *layer_i = NULL;   /* an integer layer, for a texel fetch */
   if (x->array) {
      LLVMValueRef layer = ARG(at);
      at++;
      if (!layer)
         unsupported(t, "an array sample without its layer");
      nir_def *l = air_def(t, layer);
      if (air_is_int(layer))
         layer_i = air_int32(t, layer);
      l = air_is_int(layer) ? nir_u2f32(b, air_int32(t, layer)) : l->bit_size == 32 ? l : nir_f2f32(b, l);
      coord = air_append(b, coord, l);
   }
   nir_def *comparator = NULL;
   if (compare) {
      LLVMValueRef ref = ARG(at);
      at++;
      if (!ref || !air_is_float_scalar(ref))
         unsupported(t, "%s without its reference", name);
      comparator = air_def(t, ref);
      if (comparator->bit_size != 32)
         comparator = nir_f2f32(b, comparator);
   }
   bool grad = strstr(name, "_grad") != NULL;
   nir_def *ddx = NULL, *ddy = NULL, *lod = NULL, *bias = NULL, *offset = NULL, *dyn_offset = NULL;
   bool seen_offset = false, implicit = false;
   if (grad) {
      ddx = air_def(t, ARG(at));
      ddy = air_def(t, ARG(at + 1));
      LLVMValueRef clamp = ARG(at + 2);
      if (!ddx || !ddy)
         unsupported(t, "%s without its gradients", name);
      if (clamp && air_is_float_scalar(clamp) && !air_is_zero(clamp))
         unsupported(t, "a gradient sample with a minimum LOD clamp");
      at += 3;
   }
   for (unsigned i = at; i < nargs; i++) {
      LLVMValueRef v = ARG(i);
      LLVMTypeRef ty = L.LLVMTypeOf(v);
      /* the offset: the first integer vector with the texture's spatial width */
      if (!seen_offset && L.LLVMGetTypeKind(ty) == TY_VECTOR && air_is_int(v) && L.LLVMGetVectorSize(ty) == spatial &&
          x->dim != GLSL_SAMPLER_DIM_CUBE) {
         seen_offset = true;
         if (!air_is_zero(v)) {
            /* an offset known only at run time moves the coordinate by offset / size, as the translator does
             * (apply_dynamic_sample_offset, the size at level 0) */
            if (!L.LLVMIsAConstantVector(v) && !L.LLVMIsAConstantDataSequential(v))
               dyn_offset = air_int32(t, v);
            else
               offset = air_int32(t, v);
         }
         continue;
      }
      /* the level: the first float scalar, after the bool that says whether it is a level (true) or a bias */
      if (!grad && !lod && air_is_float_scalar(v) && i > at && air_is_bool(ARG(i - 1))) {
         LLVMValueRef flag = ARG(i - 1);
         bool level = !L.LLVMIsAConstantInt(flag) || (L.LLVMConstIntGetZExtValue(flag) & 1);
         if (!level && t->stage == NVMTL_AIR_NIR_STAGE_FRAGMENT) {
            /* a fragment function samples at the level its derivatives give, moved by the bias */
            implicit = true;
            if (!air_is_zero(v)) {
               bias = air_def(t, v);
               bias = bias->bit_size == 32 ? bias : nir_f2f32(b, bias);
            }
         } else {
            if (!level && !air_is_zero(v))
               unsupported(t, "a sample with a bias outside a fragment function (no derivatives there)");
            lod = air_def(t, v);
            lod = !level ? nir_imm_float(b, 0) : lod->bit_size == 32 ? lod : nir_f2f32(b, lod);
         }
         i++;   /* the float after the level (a minimum LOD clamp) */
         if (i < nargs && air_is_float_scalar(ARG(i)) && !air_is_zero(ARG(i)))
            unsupported(t, "a sample with a minimum LOD clamp");
      }
   }
   nir_deref_instr *tex_deref = air_texture_deref(t, x);
   if (x->base != GLSL_TYPE_FLOAT && !gather && !air_sampler_pixel(s)) {
      /* an integer texture sampled: as the translator does (images/sample.rs, fetch_coord.rs
       * build_normalized_nearest_fetch_coord), a sampler whose state is known here (constexpr, or given by the request)
       * fetches the nearest texel at floor(coord * size) + offset, at the level truncated (0 for a bias), each axis by
       * its address mode, and zero outside where the mode clamps to zero; the layer clamped to the last one. An
       * argument sampler whose state is not known samples it in the hardware, as the translator's does. */
      if (grad)
         unsupported(t, "a gradient sample of an integer texture (the translator's nearest fetch has no place for it)");
      if (s->is_static || s->given) {
         const uint64_t w = s->words[0];
         const bool border_black = ((w >> 56) & 3) == 0;
         if (x->dim != GLSL_SAMPLER_DIM_1D && x->dim != GLSL_SAMPLER_DIM_2D && x->dim != GLSL_SAMPLER_DIM_3D)
            unsupported(t, "a sample of an integer %s through a sampler known here (the translator fetches only 1D, 2D "
                        "and 3D ones)", x->type_name ? x->type_name : "texture");
         if (x->array && !layer_i)
            unsupported(t, "an integer array texture sampled at a float layer");
         nir_def *lod_u = lod ? nir_f2u32(b, lod) : nir_imm_int(b, 0);
         struct air_tex_srcs q = { .texture = tex_deref, .lod = lod_u };
         nir_def *size = air_build_tex(t, nir_texop_txs, x, &q, -1);
         nir_def *comps[4], *in_bounds = NULL;
         for (unsigned i = 0; i < spatial; i++) {
            const unsigned mode = (w >> (3 * i)) & 7;
            if (mode > 3)
               unsupported(t, "a sampler address mode %u the translator does not decode", mode);
            nir_def *sz = nir_channel(b, size, i);
            nir_def *ci = nir_f2i32(b, nir_ffloor(b, nir_fmul(b, nir_channel(b, coord, i), nir_u2f32(b, sz))));
            if (offset)
               ci = nir_iadd(b, ci, nir_channel(b, offset, i));
            else if (dyn_offset)
               ci = nir_iadd(b, ci, nir_channel(b, dyn_offset, i));
            nir_def *below = nir_ilt_imm(b, ci, 0);
            nir_def *nonneg = nir_bcsel(b, below, nir_imm_int(b, 0), ci);
            nir_def *max = nir_iadd_imm(b, sz, -1);
            nir_def *above = nir_ilt(b, max, nonneg);
            nir_def *clamped = nir_bcsel(b, above, max, nonneg);
            if (mode == 2) {
               comps[i] = nir_imod(b, ci, sz);
            } else if (mode == 3) {
               nir_def *period = nir_imul_imm(b, sz, 2);
               nir_def *phase = nir_imod(b, ci, period);
               nir_def *mirrored = nir_isub(b, nir_iadd_imm(b, period, -1), phase);
               comps[i] = nir_bcsel(b, nir_ige(b, phase, sz), mirrored, phase);
            } else {
               comps[i] = clamped;
            }
            if (mode == 0 && border_black) {
               nir_def *inside = nir_iand(b, nir_inot(b, below), nir_inot(b, above));
               in_bounds = in_bounds ? nir_iand(b, in_bounds, inside) : inside;
            }
         }
         nir_def *fc = nir_vec(b, comps, spatial);
         if (x->array) {
            nir_def *count = nir_channel(b, size, spatial);
            fc = air_append(b, fc, nir_bcsel(b, nir_ult(b, layer_i, count), layer_i, nir_iadd_imm(b, count, -1)));
         }
         struct air_tex_srcs fin = { .texture = tex_deref, .coord = fc, .lod = lod_u };
         nir_def *color = air_build_tex(t, nir_texop_txf, x, &fin, -1);
         if (in_bounds)
            color = nir_bcsel(b, in_bounds, color, nir_imm_zero(b, color->num_components, 32));
         *out = air_texel_result(t, call, color);
         return true;
      }
   }
   nir_def *size0 = NULL;
   if (dyn_offset || air_sampler_pixel(s)) {
      struct air_tex_srcs q = { .texture = tex_deref, .lod = nir_imm_int(b, 0) };
      size0 = nir_i2f32(b, nir_trim_vector(b, air_build_tex(t, nir_texop_txs, x, &q, -1), spatial));
   }
   if (dyn_offset) {
      nir_def *sp = nir_trim_vector(b, coord, spatial);
      sp = nir_fadd(b, sp, nir_fdiv(b, nir_i2f32(b, dyn_offset), size0));
      coord = x->array ? air_append(b, sp, nir_channel(b, coord, spatial)) : sp;
   }
   if (air_sampler_pixel(s)) {
      /* a pixel-coordinate sampler (constexpr, or bound with its state in the request): the plugin's VkSampler for it is
       * normalized with the same filter and address modes (the translator says so of its own, and of the one a
       * pixel-coordinate MTLSamplerDescriptor makes), so a linear sample of a 2D texture at level 0 with
       * clamp_to_edge is the hardware's at coord / size; a nearest one fetches floor(coord) clamped, or zero outside
       * with clamp_to_zero, as the translator's pixel_fetch does */
      uint64_t w = s->words[0];
      unsigned as = w & 7, at = (w >> 3) & 7, ar = (w >> 6) & 7;
      bool nearest = ((w >> 9) & 3) == 0 && ((w >> 11) & 3) == 0;
      if (x->dim == GLSL_SAMPLER_DIM_CUBE || grad || comparator || (lod && !nir_src_is_const(nir_src_for_ssa(lod))) ||
          (lod && nir_src_as_float(nir_src_for_ssa(lod)) != 0.0) || (x->array && (gather || !nearest || !layer_i)))
         unsupported(t, "a pixel-coordinate sample of this shape (a cube, a gradient, a comparison, a level, or a linear "
                     "or gathered array)");
      if (gather || !nearest) {
         if (x->dim != GLSL_SAMPLER_DIM_2D || as != 1 || at != 1)
            unsupported(t, "a linear pixel-coordinate sample other than 2D clamp_to_edge (the translator emulates it)");
         coord = nir_fdiv(b, coord, size0);
      } else {
         bool zero_outside = as == 0 || at == 0 || (spatial == 3 && ar == 0);
         if ((as != 0 && as != 1) || (at != 0 && at != 1) || (spatial == 3 && ar != 0 && ar != 1))
            unsupported(t, "a pixel-coordinate sampler with an address mode Metal does not allow for one");
         nir_def *ip = nir_f2i32(b, nir_ffloor(b, nir_trim_vector(b, coord, spatial)));
         nir_def *isize = nir_f2i32(b, size0);
         nir_def *clamped = nir_imin(b, nir_imax(b, ip, nir_imm_int(b, 0)), nir_iadd_imm(b, isize, -1));
         struct air_tex_srcs fin = { .texture = tex_deref, .coord = x->array ? air_append(b, clamped, layer_i) : clamped,
                                     .lod = nir_imm_int(b, 0) };
         nir_def *color = air_build_tex(t, nir_texop_txf, x, &fin, -1);
         if (zero_outside) {
            nir_def *in = nir_ball(b, nir_ieq(b, ip, clamped));
            color = nir_bcsel(b, in, color, nir_imm_zero(b, color->num_components, 32));
         }
         *out = air_texel_result(t, call, color);
         return true;
      }
   }
   if (gather) {
      if (x->depth || grad)
         unsupported(t, "%s", name);
      /* (texture, sampler, coord, [layer,] bool, offset, component, ...) */
      LLVMValueRef comp = ARG(x->array ? 6 : 5);
      if (!comp || !L.LLVMIsAConstantInt(comp) || L.LLVMConstIntGetZExtValue(comp) > 3)
         unsupported(t, "a gather of a component not known here");
      struct air_tex_srcs in = { .texture = tex_deref, .sampler = air_sampler_deref(t, s, false),
                                 .coord = coord, .offset = offset };
      *out = air_texel_result(t, call, air_build_tex(t, nir_texop_tg4, x, &in, (int)L.LLVMConstIntGetZExtValue(comp)));
      return true;
   }
   if (s->is_static && comparator && ((s->words[0] >> 16) & 0xf) == 0)
      unsupported(t, "a compare sample through a constexpr sampler without a compare function");
   struct air_tex_srcs in = { .texture = tex_deref, .sampler = air_sampler_deref(t, s, comparator != NULL),
                              .coord = coord, .lod = grad || implicit ? NULL : lod ? lod : nir_imm_float(b, 0),
                              .bias = bias, .comparator = comparator, .ddx = ddx, .ddy = ddy, .offset = offset };
   if (ddx && (ddx->num_components != spatial || ddy->num_components != spatial))
      unsupported(t, "gradients of %u components", ddx->num_components);
   if (ddx && ddx->bit_size != 32) {
      in.ddx = nir_f2f32(b, ddx);
      in.ddy = nir_f2f32(b, ddy);
   }
   nir_def *color = air_build_tex(t, grad ? nir_texop_txd : implicit ? (bias ? nir_texop_txb : nir_texop_tex) : nir_texop_txl,
                                  x, &in, -1);
   *out = air_texel_result(t, call, color);
   return true;
#undef ARG
}

static void
air_call(struct air_tr *t, LLVMValueRef call)
{
   nir_builder *b = &t->b;
   LLVMValueRef callee = L.LLVMGetCalledValue(call);
   if (!callee || !L.LLVMIsAFunction(callee))
      unsupported(t, "an indirect call");
   const char *name = air_name(callee, NULL);
   unsigned nargs = L.LLVMGetNumArgOperands(call);
   LLVMTypeRef rlty = L.LLVMTypeOf(call);
   bool returns = L.LLVMGetTypeKind(rlty) != TY_VOID;

   if (!L.LLVMIsDeclaration(callee))
      unsupported(t, "a call to %s, which LLVM did not inline", name);

   if (!strcmp(name, "air.discard_fragment")) {
      /* a demote, as the translator's (OpDemoteToHelperInvocation): derivatives after it stay defined */
      if (t->stage != NVMTL_AIR_NIR_STAGE_FRAGMENT)
         unsupported(t, "discard_fragment outside a fragment function");
      nir_demote(b);
      t->fragment_flags |= NVMTL_AIR_NIR_FS_DISCARD;
      return;
   }

   struct air_val *tex_result;
   if (air_texture_call(t, name, call, nargs, &tex_result)) {
      if (returns) {
         if (!tex_result)
            unsupported(t, "%s gives no value here", name);
         air_set_val(t, call, tex_result);
      }
      return;
   }
   struct air_type rt = { 0 };
   if (returns)
      rt = air_type_of(t, rlty);

   /* operands that are plain values; pointers stay with their kinds */
   nir_def *s[8] = { NULL };
   bool value_args = !air_starts(name, "llvm.mem") && !air_starts(name, "air.atomic.") &&
                     !air_starts(name, "llvm.lifetime") && !air_starts(name, "llvm.dbg") &&
                     !air_starts(name, "llvm.experimental") && !air_starts(name, "llvm.invariant") &&
                     !air_starts(name, "llvm.assume");
   if (value_args) {
      if (nargs > 8)
         unsupported(t, "a call to %s with %u operands", name, nargs);
      for (unsigned i = 0; i < nargs; i++) {
         struct air_val *v = air_get(t, L.LLVMGetOperand(call, i));
         s[i] = air_vdef(t, v);
      }
   }

   nir_def *r = NULL;
   if (air_starts(name, "llvm.")) {
      bool handled;
      r = air_llvm_intrinsic(t, name, call, s, nargs, &handled);
      if (!handled)
         unsupported(t, "the LLVM intrinsic %s", name);
   } else if (air_starts(name, "air.wg.barrier") || air_starts(name, "air.simdgroup.barrier")) {
      uint64_t flags = 0, scope = 1;
      if (nargs >= 1 && L.LLVMIsAConstantInt(L.LLVMGetOperand(call, 0)))
         flags = L.LLVMConstIntGetZExtValue(L.LLVMGetOperand(call, 0));
      if (nargs >= 2 && L.LLVMIsAConstantInt(L.LLVMGetOperand(call, 1)))
         scope = L.LLVMConstIntGetZExtValue(L.LLVMGetOperand(call, 1));
      bool simd = air_starts(name, "air.simdgroup.");
      air_barrier(t, flags, simd ? SCOPE_SUBGROUP : SCOPE_WORKGROUP, simd ? SCOPE_SUBGROUP : air_scope(scope));
   } else if (air_starts(name, "air.atomic.fence")) {
      uint64_t flags = 0, scope = 1;
      if (nargs >= 1 && L.LLVMIsAConstantInt(L.LLVMGetOperand(call, 0)))
         flags = L.LLVMConstIntGetZExtValue(L.LLVMGetOperand(call, 0));
      if (nargs >= 3 && L.LLVMIsAConstantInt(L.LLVMGetOperand(call, 2)))
         scope = L.LLVMConstIntGetZExtValue(L.LLVMGetOperand(call, 2));
      air_barrier(t, flags, SCOPE_NONE, air_scope(scope));
   } else if (air_starts(name, "air.atomic.")) {
      bool void_result;
      r = air_atomic(t, name + strlen("air.atomic."), call, nargs, &void_result);
      if (void_result)
         r = NULL;
   } else if (air_starts(name, "air.convert.")) {
      if (!returns || nargs != 1)
         unsupported(t, "%s", name);
      r = air_convert(t, name + strlen("air.convert."), s[0], &rt);
   } else if ((air_starts(name, "air.pack.") || air_starts(name, "air.unpack.")) && returns && nargs == 1) {
      r = air_pack(t, name, s[0], &rt);
   } else if (air_starts(name, "air.get_") && nargs == 1) {
      /* OpenCL's work-item functions, as Apple's OpenCL compiler writes them in AIR */
      static const struct { const char *cl, *metal; } map[] = {
         { "global_id.", "air.thread_position_in_grid" },
         { "local_id.", "air.thread_position_in_threadgroup" },
         { "group_id.", "air.threadgroup_position_in_grid" },
         { "local_size.", "air.threads_per_threadgroup" },
         { "global_size.", "air.threads_per_grid" },
         { "num_groups.", "air.threadgroups_per_grid" },
      };
      nir_def *v = NULL;
      for (unsigned i = 0; i < ARRAY_SIZE(map) && !v; i++) {
         if (air_starts(name + strlen("air.get_"), map[i].cl))
            v = air_builtin(t, map[i].metal);
      }
      if (!v)
         unsupported(t, "the AIR function %s", name);
      r = nir_vector_extract(b, v, nir_u2u32(b, s[0]));
   } else if (air_starts(name, "air.is_uniform.") && returns && nargs == 1) {
      /* whether the value is the same in every thread of the SIMD-group (Apple's compiler writes it for
       * metal::uniform<T>, mostly to feed llvm.assume) */
      nir_def *v = s[0];
      if (v->bit_size == 1)
         v = nir_b2i32(b, v);
      else if (v->bit_size < 32)
         v = nir_u2u32(b, v);
      nir_def *same = nir_imm_true(b);
      for (unsigned i = 0; i < v->num_components; i++)
         same = nir_iand(b, same, nir_vote_ieq(b, 1, nir_channel(b, v, i)));
      r = same;
   } else if (air_starts(name, "air.simd_") || air_starts(name, "air.quad_")) {
      if (!returns)
         unsupported(t, "%s gives no value", name);
      r = air_simd(t, name, call, s, nargs, &rt);
   } else if (air_starts(name, "air.any.") || air_starts(name, "air.all.")) {
      const bool any = air_starts(name, "air.any.");
      nir_def *v = s[0]->bit_size == 1 ? s[0] : nir_ine_imm(b, s[0], 0);
      r = nir_channel(b, v, 0);
      for (unsigned i = 1; i < v->num_components; i++)
         r = any ? nir_ior(b, r, nir_channel(b, v, i)) : nir_iand(b, r, nir_channel(b, v, i));
   } else if (air_starts(name, "air.")) {
      /* air.[fast_]<function>.<type> or air.<function>.<s|u>.<type> */
      char fn[48];
      const char *p = name + 4;
      t->math_fast = air_starts(p, "fast_");
      if (t->math_fast)
         p += 5;
      const char *dot = strchr(p, '.');
      if (!dot || (size_t)(dot - p) >= sizeof fn)
         unsupported(t, "the AIR function %s", name);
      memcpy(fn, p, dot - p);
      fn[dot - p] = 0;
      bool sign_s = air_starts(dot, ".s."), sign_u = air_starts(dot, ".u.");
      bool float_args = nargs > 0 && L.LLVMGetTypeKind(L.LLVMTypeOf(L.LLVMGetOperand(call, 0))) != TY_POINTER &&
                        air_type_of(t, L.LLVMTypeOf(L.LLVMGetOperand(call, 0))).is_float;
      if (!strcmp(fn, "sincos") && nargs == 2) {
         /* air.[fast_]sincos(x, ptr cos): sin returned, cos stored */
         struct air_val *p = air_get(t, L.LLVMGetOperand(call, 1));
         nir_def *x = s[0]->bit_size == 32 ? s[0] : nir_f2f32(b, s[0]);
         nir_def *c = nir_fcos(b, x);
         air_store_mem(t, p, s[0]->bit_size == 32 ? c : nir_f2fN(b, c, s[0]->bit_size), s[0]->bit_size / 8);
         r = air_narrow(b, nir_fsin(b, x), s[0]->bit_size);
      } else if (float_args && !sign_s && !sign_u)
         r = air_float_function(t, fn, s, nargs, &rt);
      else
         r = air_integer_function(t, fn, sign_s, s, nargs);
      if (!r)
         unsupported(t, "the AIR function %s", name);
   } else {
      unsupported(t, "a call to %s", name);
   }

   if (returns) {
      if (!r)
         unsupported(t, "%s gives no value here", name);
      if (rt.bits == 1 && r->bit_size != 1)
         r = nir_ine_imm(b, r, 0);
      else if (r->bit_size != rt.bits)
         r = rt.is_float ? nir_f2fN(b, r, rt.bits) : nir_u2uN(b, r, rt.bits);
      if (r->num_components != rt.components)
         unsupported(t, "%s gives %u components where LLVM expects %u", name, r->num_components, rt.components);
      air_set(t, call, r, AIR_VALUE, 0);
   }
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Instructions and the CFG                                                                                           */

static struct air_block *
air_block(struct air_tr *t, LLVMBasicBlockRef bb, struct list_head *work)
{
   struct hash_entry *e = _mesa_hash_table_search(t->blocks, bb);
   if (e)
      return e->data;
   struct air_block *blk = rzalloc(t->mem, struct air_block);
   blk->llvm = bb;
   blk->nir = nir_block_create(t->impl);
   exec_list_push_tail(&t->impl->body, &blk->nir->cf_node.node);
   blk->nir->cf_node.parent = &t->impl->cf_node;
   _mesa_hash_table_insert(t->blocks, bb, blk);
   list_addtail(&blk->link, work);
   return blk;
}

static struct air_val *
air_select(struct air_tr *t, nir_def *c, struct air_val *x, struct air_val *y)
{
   if (x->kind != y->kind)
      unsupported(t, "a select between pointers of two kinds");
   if ((x->kind == AIR_TEXTURE || x->kind == AIR_SAMPLER) && x->res == y->res)
      return x;   /* the same handle either way */
   if (x->kind != AIR_AGGREGATE)
      return air_make(t, nir_bcsel(&t->b, c, air_vdef(t, x), air_vdef(t, y)), x->kind, MIN2(x->align, y->align));
   if (x->num_elems != y->num_elems)
      unsupported(t, "a select between two shapes of aggregate");
   struct air_val *r = air_aggregate(t, x->num_elems);
   for (unsigned i = 0; i < x->num_elems; i++)
      r->elems[i] = air_select(t, c, x->elems[i], y->elems[i]);
   return r;
}

static void
air_instruction(struct air_tr *t, LLVMValueRef inst)
{
   nir_builder *b = &t->b;
   int op = L.LLVMGetInstructionOpcode(inst);
   LLVMTypeRef lty = L.LLVMTypeOf(inst);
   t->instructions++;

   switch (op) {
   case OP_ADD: case OP_SUB: case OP_MUL: case OP_UDIV: case OP_SDIV: case OP_UREM: case OP_SREM: case OP_SHL:
   case OP_LSHR: case OP_ASHR: case OP_AND: case OP_OR: case OP_XOR: case OP_FADD: case OP_FSUB: case OP_FMUL:
   case OP_FDIV: case OP_FREM: {
      struct air_val *x = air_get(t, L.LLVMGetOperand(inst, 0)), *y = air_get(t, L.LLVMGetOperand(inst, 1));
      struct air_type ty = air_type_of(t, lty);
      nir_def *xd = air_vdef(t, x), *yd = air_vdef(t, y);
      if (ty.width != ty.bits && !ty.is_float) {
         if (op == OP_SDIV || op == OP_SREM || op == OP_ASHR)
            xd = air_signed(b, xd, ty.width);
         if (op == OP_SDIV || op == OP_SREM)
            yd = air_signed(b, yd, ty.width);
      }
      air_set(t, inst, air_canon(b, air_binop(t, op, xd, yd), ty.width), AIR_VALUE, 0);
      return;
   }
   case OP_FNEG:
      air_set(t, inst, nir_fneg(b, air_def(t, L.LLVMGetOperand(inst, 0))), AIR_VALUE, 0);
      return;
   case OP_FREEZE:
      air_set_val(t, inst, air_get(t, L.LLVMGetOperand(inst, 0)));
      return;
   case OP_EXTRACTVALUE: {
      struct air_val *v = air_get(t, L.LLVMGetOperand(inst, 0));
      unsigned n = L.LLVMGetNumIndices(inst);
      const unsigned *idx = L.LLVMGetIndices(inst);
      for (unsigned i = 0; i < n; i++) {
         if (v->kind != AIR_AGGREGATE || idx[i] >= v->num_elems)
            unsupported(t, "an extractvalue outside its aggregate");
         v = v->elems[idx[i]];
      }
      air_set_val(t, inst, v);
      return;
   }
   case OP_INSERTVALUE: {
      /* a copy of the aggregate down the indices, the element at their end replaced */
      struct air_val *root = air_get(t, L.LLVMGetOperand(inst, 0)), *elem = air_get(t, L.LLVMGetOperand(inst, 1));
      unsigned n = L.LLVMGetNumIndices(inst);
      const unsigned *idx = L.LLVMGetIndices(inst);
      if (!n)
         unsupported(t, "an insertvalue without indices");
      struct air_val *copy = NULL, *parent = NULL;
      for (unsigned i = 0; i < n; i++) {
         if (root->kind != AIR_AGGREGATE || idx[i] >= root->num_elems)
            unsupported(t, "an insertvalue outside its aggregate");
         struct air_val *c = air_aggregate(t, root->num_elems);
         memcpy(c->elems, root->elems, root->num_elems * sizeof *c->elems);
         if (parent)
            parent->elems[idx[i - 1]] = c;
         else
            copy = c;
         parent = c;
         root = root->elems[idx[i]];
      }
      parent->elems[idx[n - 1]] = elem;
      air_set_val(t, inst, copy);
      return;
   }
   case OP_ICMP: {
      struct air_val *x = air_get(t, L.LLVMGetOperand(inst, 0)), *y = air_get(t, L.LLVMGetOperand(inst, 1));
      if (air_vdef(t, x)->bit_size != air_vdef(t, y)->bit_size)
         unsupported(t, "a comparison of pointers of two kinds");
      int pred = L.LLVMGetICmpPredicate(inst);
      unsigned width = air_width_of(t, L.LLVMGetOperand(inst, 0));
      nir_def *xd = air_vdef(t, x), *yd = air_vdef(t, y);
      if (pred >= ICMP_SGT) {
         xd = air_signed(b, xd, width);
         yd = air_signed(b, yd, width);
      }
      air_set(t, inst, air_icmp(t, pred, xd, yd), AIR_VALUE, 0);
      return;
   }
   case OP_FCMP:
      air_set(t, inst, air_fcmp(t, L.LLVMGetFCmpPredicate(inst), air_def(t, L.LLVMGetOperand(inst, 0)),
                                air_def(t, L.LLVMGetOperand(inst, 1))), AIR_VALUE, 0);
      return;
   case OP_SELECT: {
      nir_def *c = air_def(t, L.LLVMGetOperand(inst, 0));
      air_set_val(t, inst, air_select(t, c, air_get(t, L.LLVMGetOperand(inst, 1)), air_get(t, L.LLVMGetOperand(inst, 2))));
      return;
   }
   case OP_TRUNC: case OP_ZEXT: case OP_SEXT: case OP_FPTOUI: case OP_FPTOSI: case OP_UITOFP: case OP_SITOFP:
   case OP_FPTRUNC: case OP_FPEXT: {
      struct air_type to = air_type_of(t, lty);
      LLVMValueRef src = L.LLVMGetOperand(inst, 0);
      nir_def *x = air_def(t, src);
      if (op == OP_SEXT || op == OP_SITOFP)
         x = air_signed(b, x, air_width_of(t, src));
      air_set(t, inst, air_canon(b, air_cast(t, op, x, &to), to.width), AIR_VALUE, 0);
      return;
   }
   case OP_BITCAST: {
      struct air_type to = air_type_of(t, lty);
      struct air_val *x = air_get(t, L.LLVMGetOperand(inst, 0));
      if (to.is_pointer)
         air_set(t, inst, x->def, x->kind, x->align)->res = x->res;
      else
         air_set(t, inst, air_bitcast(t, air_vdef(t, x), &to), AIR_VALUE, 0);
      return;
   }
   case OP_ADDRSPACECAST: {
      struct air_type to = air_type_of(t, lty);
      struct air_val *x = air_get(t, L.LLVMGetOperand(inst, 0));
      if (air_kind_of_space(to.address_space) != x->kind)
         unsupported(t, "a cast from one address space to another");
      air_set(t, inst, x->def, x->kind, x->align);
      return;
   }
   case OP_PTRTOINT: {
      struct air_type to = air_type_of(t, lty);
      struct air_val *x = air_get(t, L.LLVMGetOperand(inst, 0));
      air_set(t, inst, air_canon(b, air_resize(b, air_vdef(t, x), to.bits, false), to.width), AIR_VALUE, 0);
      return;
   }
   case OP_INTTOPTR: {
      struct air_type to = air_type_of(t, lty);
      nir_def *x = air_def(t, L.LLVMGetOperand(inst, 0));
      air_set(t, inst, air_resize(b, x, to.bits, false), air_kind_of_space(to.address_space), 1);
      return;
   }
   case OP_GEP:
      air_gep(t, b, inst, inst);
      return;
   case OP_ALLOCA: {
      LLVMTypeRef aty = L.LLVMGetAllocatedType(inst);
      if (L.LLVMGetNumOperands(inst) > 0 && !L.LLVMIsAConstantInt(L.LLVMGetOperand(inst, 0)))
         unsupported(t, "an alloca of a size known only at run time");
      uint64_t count = L.LLVMGetNumOperands(inst) > 0 ? L.LLVMConstIntGetZExtValue(L.LLVMGetOperand(inst, 0)) : 1;
      uint64_t size = air_size(t, aty) * count;
      uint32_t align = MAX2(MAX2(L.LLVMGetAlignment(inst), L.LLVMABIAlignmentOfType(t->layout, aty)), 4);
      align = MIN2(align, 16);
      t->scratch_size = ALIGN_POT(t->scratch_size, align);
      if (t->scratch_size + size > 64 * 1024)
         unsupported(t, "%llu bytes of thread memory", (unsigned long long)(t->scratch_size + size));
      air_set(t, inst, nir_imm_int(&t->top, t->scratch_size), AIR_SCRATCH, align);
      t->scratch_size += (uint32_t)size;
      return;
   }
   case OP_LOAD: {
      struct air_val *p = air_get(t, L.LLVMGetOperand(inst, 0));
      air_load(t, inst, p, lty, L.LLVMGetAlignment(inst));
      return;
   }
   case OP_STORE: {
      struct air_val *p = air_get(t, L.LLVMGetOperand(inst, 1));
      air_store(t, p, L.LLVMGetOperand(inst, 0), L.LLVMGetAlignment(inst));
      return;
   }
   case OP_EXTRACTELEMENT: {
      nir_def *v = air_def(t, L.LLVMGetOperand(inst, 0));
      LLVMValueRef index = L.LLVMGetOperand(inst, 1);
      nir_def *r;
      if (L.LLVMIsAConstantInt(index)) {
         uint64_t i = L.LLVMConstIntGetZExtValue(index);
         r = i < v->num_components ? nir_channel(b, v, (unsigned)i) : nir_undef(b, 1, v->bit_size);
      } else {
         r = nir_vector_extract(b, v, nir_u2u32(b, air_def(t, index)));
      }
      air_set(t, inst, r, AIR_VALUE, 0);
      return;
   }
   case OP_INSERTELEMENT: {
      nir_def *v = air_def(t, L.LLVMGetOperand(inst, 0)), *e = air_def(t, L.LLVMGetOperand(inst, 1));
      LLVMValueRef index = L.LLVMGetOperand(inst, 2);
      nir_def *r;
      if (L.LLVMIsAConstantInt(index)) {
         uint64_t i = L.LLVMConstIntGetZExtValue(index);
         r = i < v->num_components ? nir_vector_insert_imm(b, v, e, (unsigned)i) : v;
      } else {
         r = nir_vector_insert(b, v, e, nir_u2u32(b, air_def(t, index)));
      }
      air_set(t, inst, r, AIR_VALUE, 0);
      return;
   }
   case OP_SHUFFLEVECTOR: {
      nir_def *x = air_def(t, L.LLVMGetOperand(inst, 0)), *y = air_def(t, L.LLVMGetOperand(inst, 1));
      unsigned n = L.LLVMGetNumMaskElements(inst);
      if (n < 1 || (n > 4 && n != 8 && n != 16))
         unsupported(t, "a shuffle into %u components", n);
      nir_def *c[NIR_MAX_VEC_COMPONENTS];
      for (unsigned i = 0; i < n; i++) {
         int m = L.LLVMGetMaskValue(inst, i);
         if (m < 0)
            c[i] = nir_undef(b, 1, x->bit_size);
         else if ((unsigned)m < x->num_components)
            c[i] = nir_channel(b, x, m);
         else if ((unsigned)m - x->num_components < y->num_components)
            c[i] = nir_channel(b, y, m - x->num_components);
         else
            c[i] = nir_undef(b, 1, x->bit_size);
      }
      air_set(t, inst, nir_vec(b, c, n), AIR_VALUE, 0);
      return;
   }
   case OP_CALL:
      air_call(t, inst);
      return;
   default:
      unsupported(t, "the LLVM instruction with opcode %d", op);
   }
}

static void
air_terminator(struct air_tr *t, struct air_block *blk, LLVMValueRef term, struct list_head *work)
{
   nir_builder *b = &t->b;
   blk->end = nir_nop(b);
   switch (L.LLVMGetInstructionOpcode(term)) {
   case OP_RET:
      if (t->stage != NVMTL_AIR_NIR_STAGE_KERNEL && L.LLVMGetNumOperands(term) > 0)
         air_store_outputs(t, L.LLVMGetOperand(term, 0));
      nir_goto(b, t->impl->end_block);
      return;
   case OP_UNREACHABLE:
      nir_goto(b, t->impl->end_block);
      return;
   case OP_BR:
      if (L.LLVMIsConditional(term)) {
         nir_def *c = air_def(t, L.LLVMGetCondition(term));
         struct air_block *yes = air_block(t, L.LLVMGetSuccessor(term, 0), work);
         struct air_block *no = air_block(t, L.LLVMGetSuccessor(term, 1), work);
         if (yes == no)
            nir_goto(b, yes->nir);
         else
            nir_goto_if(b, yes->nir, c, no->nir);
      } else {
         nir_goto(b, air_block(t, L.LLVMGetSuccessor(term, 0), work)->nir);
      }
      return;
   case OP_SWITCH: {
      nir_def *sel = air_def(t, L.LLVMGetOperand(term, 0));
      unsigned cases = L.LLVMGetNumSuccessors(term);
      for (unsigned i = 1; i < cases; i++) {
         LLVMValueRef value = L.LLVMGetOperand(term, 2 * i);
         if (!L.LLVMIsAConstantInt(value))
            unsupported(t, "a switch case that is not a constant");
         struct air_block *dest = air_block(t, L.LLVMGetSuccessor(term, i), work);
         nir_def *c = nir_ieq_imm(b, sel, L.LLVMConstIntGetZExtValue(value));
         nir_block *next = nir_block_create(t->impl);
         exec_list_push_tail(&t->impl->body, &next->cf_node.node);
         next->cf_node.parent = &t->impl->cf_node;
         nir_goto_if(b, dest->nir, c, next);
         b->cursor = nir_after_block(next);
      }
      nir_goto(b, air_block(t, L.LLVMGetSuccessor(term, 0), work)->nir);
      return;
   }
   default:
      unsupported(t, "the terminator with LLVM opcode %d", L.LLVMGetInstructionOpcode(term));
   }
}

/* A phi's variable, one per element of an aggregate phi (each with the indices down to it), read where the phi is. */
static struct air_val *
air_phi_value(struct air_tr *t, struct air_phi *phi, LLVMTypeRef lty)
{
   if (air_is_aggregate(lty)) {
      if (phi->depth >= AIR_MAX_DEPTH)
         unsupported(t, "a phi of aggregates nested %u deep", phi->depth);
      unsigned n = air_num_elems(t, lty);
      struct air_val *r = air_aggregate(t, n);
      for (unsigned i = 0; i < n; i++) {
         struct air_phi sub = *phi;
         sub.path[sub.depth++] = (uint16_t)i;
         r->elems[i] = air_phi_value(t, &sub, air_elem_type(lty, i));
      }
      return r;
   }
   struct air_type ty = air_type_of(t, lty);
   phi->var = nir_local_variable_create(t->impl, air_glsl_type(&ty), "phi");
   phi->kind = ty.is_pointer ? air_kind_of_space(ty.address_space) : AIR_VALUE;
   util_dynarray_append(&t->phis, *phi);
   return air_make(t, nir_load_var(&t->b, phi->var), phi->kind, 1);
}

static void
air_emit_function(struct air_tr *t)
{
   struct list_head work;
   list_inithead(&work);
   LLVMBasicBlockRef entry = L.LLVMGetEntryBasicBlock(t->function);
   struct air_block *first = rzalloc(t->mem, struct air_block);
   first->llvm = entry;
   first->nir = nir_start_block(t->impl);
   _mesa_hash_table_insert(t->blocks, entry, first);
   list_addtail(&first->link, &work);

   while (!list_is_empty(&work)) {
      struct air_block *blk = list_first_entry(&work, struct air_block, link);
      list_del(&blk->link);
      t->b.cursor = nir_after_block(blk->nir);
      LLVMValueRef term = L.LLVMGetBasicBlockTerminator(blk->llvm);
      for (LLVMValueRef inst = L.LLVMGetFirstInstruction(blk->llvm); inst && inst != term;
           inst = L.LLVMGetNextInstruction(inst)) {
         if (L.LLVMGetInstructionOpcode(inst) == OP_PHI) {
            struct air_phi phi = { .phi = inst };
            air_set_val(t, inst, air_phi_value(t, &phi, L.LLVMTypeOf(inst)));
            continue;
         }
         air_instruction(t, inst);
      }
      if (!term)
         air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "a block without a terminator");
      air_terminator(t, blk, term, &work);
   }

   util_dynarray_foreach(&t->phis, struct air_phi, phi) {
      unsigned n = L.LLVMCountIncoming(phi->phi);
      for (unsigned i = 0; i < n; i++) {
         struct hash_entry *e = _mesa_hash_table_search(t->blocks, L.LLVMGetIncomingBlock(phi->phi, i));
         struct air_block *pred = e ? e->data : NULL;
         if (!pred || !pred->end)
            continue;
         t->b.cursor = nir_after_instr(&pred->end->instr);
         struct air_val *v = air_get(t, L.LLVMGetIncomingValue(phi->phi, i));
         for (unsigned d = 0; d < phi->depth; d++) {
            if (v->kind != AIR_AGGREGATE || phi->path[d] >= v->num_elems)
               unsupported(t, "a phi of aggregates of two shapes");
            v = v->elems[phi->path[d]];
         }
         if (v->kind != phi->kind)
            unsupported(t, "a phi joining pointers of two kinds");
         nir_store_var(&t->b, phi->var, air_vdef(t, v), BITFIELD_MASK(v->def->num_components));
      }
   }
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Entry points                                                                                                       */

/* ---------------------------------------------------------------------------------------------------------------- */
/* Function constants                                                                                                 */
/*                                                                                                                    */
/* Apple's compiler writes [[function_constant(n)]] as an externally initialized global <name>.MTL_FC_INIT_<n>_<abi>   */
/* (section air.fc_initializer), and a static initializer (a function in section air.static_init) copies it, and what  */
/* is_function_constant_defined says of it, into addrspace(2) globals that are not constant (the "...Defined" flags,  */
/* __metal_implicit_fc_pred_*, the constant's own global). With the request's values each given constant becomes a    */
/* constant global, is_function_constant_defined a constant, and the static initializer is run here, so every global  */
/* it fills becomes a constant with the value it leaves; LLVM then folds the branches they decide. A global that      */
/* depends on a constant the request does not give stays unknown, and a kernel that still reads one is refused.      */

static bool
air_fc_index(LLVMValueRef g, uint32_t *index)
{
   const char *name = air_name(g, NULL);
   const char *m = strstr(name, ".MTL_FC_INIT_");
   if (!m)
      return false;
   m += strlen(".MTL_FC_INIT_");
   if (*m < '0' || *m > '9')
      return false;
   *index = (uint32_t)strtoul(m, NULL, 10);
   return true;
}

static bool
air_fc_bool_abi(LLVMValueRef g)
{
   const char *name = air_name(g, NULL);
   const char *m = strstr(name, ".MTL_FC_INIT_");
   if (!m)
      return false;
   m += strlen(".MTL_FC_INIT_");
   while (*m >= '0' && *m <= '9')
      m++;
   if (*m == '_')
      m++;
   const char *last = strrchr(m, '_');
   return !strcmp(m, "b") || (last && !strcmp(last + 1, "b"));
}

static bool
air_has_fc_values(const struct air_tr *t)
{
   return (t->req->flags & NVMTL_AIR_NIR_FC_VALUES) &&
          t->req->size >= offsetof(struct nvmtl_air_nir_request, fc_payloads) + sizeof(t->req->fc_payloads);
}

/* The payload the request gives for a constant, or NULL. */
static const uint8_t *
air_fc_given(const struct air_tr *t, uint32_t index, uint32_t *size)
{
   if (!air_has_fc_values(t))
      return NULL;
   for (uint32_t i = 0; i < t->req->fc_count; i++) {
      if (t->req->fc_indices[i] == index) {
         *size = t->req->fc_sizes[i];
         return t->req->fc_payloads[i];
      }
   }
   return NULL;
}

/* An LLVM constant of a type from bytes in memory (little endian, LLVM's data layout), or NULL where this cannot
 * say it. */
static LLVMValueRef
air_constant_from_bytes(struct air_tr *t, LLVMContextRef ctx, LLVMTypeRef ty, const uint8_t *bytes)
{
   switch (L.LLVMGetTypeKind(ty)) {
   case TY_INTEGER: {
      unsigned w = L.LLVMGetIntTypeWidth(ty);
      if (w > 64)
         return NULL;
      uint64_t v = 0;
      memcpy(&v, bytes, (w + 7) / 8);
      if (w < 64)
         v &= BITFIELD64_MASK(w);
      return L.LLVMConstInt(ty, v, 0);
   }
   case TY_HALF: {
      uint16_t h;
      memcpy(&h, bytes, 2);
      return L.LLVMConstReal(ty, _mesa_half_to_float(h));
   }
   case TY_FLOAT: {
      float f;
      memcpy(&f, bytes, 4);
      return L.LLVMConstReal(ty, f);
   }
   case TY_DOUBLE: {
      double d;
      memcpy(&d, bytes, 8);
      return L.LLVMConstReal(ty, d);
   }
   case TY_POINTER: {
      uint64_t v = 0;
      memcpy(&v, bytes, (size_t)MIN2(8, air_size(t, ty)));
      return v ? NULL : L.LLVMConstNull(ty);
   }
   case TY_VECTOR: case TY_ARRAY: {
      LLVMTypeRef ety = L.LLVMGetElementType(ty);
      uint64_t n = L.LLVMGetTypeKind(ty) == TY_VECTOR ? L.LLVMGetVectorSize(ty) : L.LLVMGetArrayLength2(ty);
      uint64_t stride = air_size(t, ety);
      if (n > 4096)
         return NULL;
      LLVMValueRef *e = ralloc_array(t->mem, LLVMValueRef, n ? n : 1);
      for (uint64_t i = 0; i < n; i++) {
         if (!(e[i] = air_constant_from_bytes(t, ctx, ety, bytes + i * stride)))
            return NULL;
      }
      return L.LLVMGetTypeKind(ty) == TY_VECTOR ? L.LLVMConstVector(e, (unsigned)n) : L.LLVMConstArray2(ety, e, n);
   }
   case TY_STRUCT: {
      unsigned n = L.LLVMCountStructElementTypes(ty);
      LLVMValueRef *e = ralloc_array(t->mem, LLVMValueRef, n ? n : 1);
      for (unsigned i = 0; i < n; i++) {
         e[i] = air_constant_from_bytes(t, ctx, L.LLVMStructGetTypeAtIndex(ty, i),
                                        bytes + L.LLVMOffsetOfElement(t->layout, ty, i));
         if (!e[i])
            return NULL;
      }
      if (L.LLVMGetStructName(ty))
         return L.LLVMConstNamedStruct(ty, e, n);
      return L.LLVMConstStructInContext(ctx, e, n, L.LLVMIsPackedStruct(ty));
   }
   default:
      return NULL;
   }
}

/* The bytes of a constant; *known cleared where it is undef. False where this cannot read the constant. */
static bool
air_bytes_of_constant(struct air_tr *t, uint8_t *out, uint8_t *known, uint64_t size, LLVMValueRef c, LLVMTypeRef ty)
{
   if (L.LLVMIsAUndefValue(c)) {
      memset(out, 0, size);
      memset(known, 0, size);
      return true;
   }
   memset(known, 1, size);
   if (L.LLVMIsAConstantAggregateZero(c) || L.LLVMIsAConstantPointerNull(c)) {
      memset(out, 0, size);
      return true;
   }
   switch (L.LLVMGetTypeKind(ty)) {
   case TY_INTEGER: {
      if (!L.LLVMIsAConstantInt(c))
         return false;
      uint64_t u = L.LLVMConstIntGetZExtValue(c);
      for (uint64_t i = 0; i < size && i < 8; i++)
         out[i] = (uint8_t)(u >> (8 * i));
      return true;
   }
   case TY_HALF: case TY_FLOAT: case TY_DOUBLE: {
      if (!L.LLVMIsAConstantFP(c))
         return false;
      int loses = 0;
      double d = L.LLVMConstRealGetDouble(c, &loses);
      if (L.LLVMGetTypeKind(ty) == TY_HALF) {
         uint16_t h = _mesa_float_to_half((float)d);
         memcpy(out, &h, 2);
      } else if (L.LLVMGetTypeKind(ty) == TY_FLOAT) {
         float f = (float)d;
         memcpy(out, &f, 4);
      } else {
         memcpy(out, &d, 8);
      }
      return true;
   }
   case TY_ARRAY: case TY_VECTOR: {
      LLVMTypeRef ety = L.LLVMGetElementType(ty);
      uint64_t stride = air_size(t, ety);
      uint64_t n = L.LLVMGetTypeKind(ty) == TY_VECTOR ? L.LLVMGetVectorSize(ty) : L.LLVMGetArrayLength2(ty);
      for (uint64_t i = 0; i < n; i++) {
         LLVMValueRef e = L.LLVMGetAggregateElement(c, (unsigned)i);
         if (!e || !air_bytes_of_constant(t, out + i * stride, known + i * stride, stride, e, ety))
            return false;
      }
      return true;
   }
   case TY_STRUCT: {
      unsigned n = L.LLVMCountStructElementTypes(ty);
      for (unsigned i = 0; i < n; i++) {
         LLVMTypeRef fty = L.LLVMStructGetTypeAtIndex(ty, i);
         uint64_t off = L.LLVMOffsetOfElement(t->layout, ty, i);
         LLVMValueRef e = L.LLVMGetAggregateElement(c, i);
         if (!e || !air_bytes_of_constant(t, out + off, known + off, air_size(t, fty), e, fty))
            return false;
      }
      return true;
   }
   default:
      return false;
   }
}

/* What the static initializer leaves in one global. */
struct air_mem {
   LLVMValueRef global;
   uint64_t size;
   uint8_t *bytes, *known;
   bool stored;
};

/* A value while the static initializer runs: up to 16 bytes, or a pointer into a global. */
struct air_ev {
   uint8_t b[16];
   unsigned size;
   bool known;
   struct air_mem *mem;   /* a pointer: the global it points into */
   uint64_t off;
};

struct air_eval {
   struct air_tr *t;
   struct hash_table *mems;     /* global -> struct air_mem */
   struct hash_table *vals;     /* instruction -> struct air_ev */
   unsigned steps;
};

static struct air_mem *
air_eval_mem(struct air_eval *e, LLVMValueRef g)
{
   struct hash_entry *he = _mesa_hash_table_search(e->mems, g);
   if (he)
      return he->data;
   struct air_tr *t = e->t;
   struct air_mem *m = rzalloc(t->mem, struct air_mem);
   LLVMTypeRef vty = L.LLVMGlobalGetValueType(g);
   m->global = g;
   m->size = air_size(t, vty);
   m->bytes = rzalloc_array(t->mem, uint8_t, m->size ? m->size : 1);
   m->known = rzalloc_array(t->mem, uint8_t, m->size ? m->size : 1);
   LLVMValueRef init = L.LLVMGetInitializer(g);
   if (init && !L.LLVMIsDeclaration(g) && !L.LLVMIsExternallyInitialized(g) && m->size <= 1u << 20 &&
       !air_bytes_of_constant(t, m->bytes, m->known, m->size, init, vty))
      memset(m->known, 0, m->size);
   _mesa_hash_table_insert(e->mems, g, m);
   return m;
}

static unsigned
air_eval_size(struct air_tr *t, LLVMTypeRef ty)
{
   if (L.LLVMGetTypeKind(ty) == TY_INTEGER && L.LLVMGetIntTypeWidth(ty) == 1)
      return 1;
   return (unsigned)L.LLVMStoreSizeOfType(t->layout, ty);
}

static uint64_t
air_ev_u64(const struct air_ev *v)
{
   uint64_t u = 0;
   memcpy(&u, v->b, MIN2(v->size, 8));
   return u;
}

static struct air_ev
air_ev_int(uint64_t u, LLVMTypeRef ty)
{
   struct air_ev r = { .known = true };
   unsigned w = L.LLVMGetIntTypeWidth(ty);
   r.size = w == 1 ? 1 : (w + 7) / 8;
   if (w < 64)
      u &= BITFIELD64_MASK(w);
   memcpy(r.b, &u, 8);
   return r;
}

/* The value of an operand: an instruction already run, a constant, or a pointer into a global. */
static struct air_ev
air_eval_value(struct air_eval *e, LLVMValueRef v)
{
   struct air_tr *t = e->t;
   struct air_ev r = { 0 };
   struct hash_entry *he = _mesa_hash_table_search(e->vals, v);
   if (he)
      return *(struct air_ev *)he->data;
   if (L.LLVMIsAGlobalVariable(v)) {
      r.mem = air_eval_mem(e, v);
      r.known = true;
      r.size = 8;
      return r;
   }
   if (L.LLVMIsAConstantExpr(v)) {
      int op = L.LLVMGetConstOpcode(v);
      if (op == OP_BITCAST || op == OP_ADDRSPACECAST)
         return air_eval_value(e, L.LLVMGetOperand(v, 0));
      if (op == OP_GEP) {
         r = air_eval_value(e, L.LLVMGetOperand(v, 0));
         if (!r.mem)
            return (struct air_ev) { 0 };
         LLVMTypeRef ty = L.LLVMGetGEPSourceElementType(v);
         unsigned n = (unsigned)L.LLVMGetNumOperands(v);
         for (unsigned i = 1; i < n; i++) {
            LLVMValueRef index = L.LLVMGetOperand(v, i);
            if (!L.LLVMIsAConstantInt(index))
               return (struct air_ev) { 0 };
            int64_t k = L.LLVMConstIntGetSExtValue(index);
            if (i == 1) {
               r.off += (uint64_t)k * air_size(t, ty);
            } else if (L.LLVMGetTypeKind(ty) == TY_STRUCT) {
               r.off += L.LLVMOffsetOfElement(t->layout, ty, (unsigned)k);
               ty = L.LLVMStructGetTypeAtIndex(ty, (unsigned)k);
            } else if (L.LLVMGetTypeKind(ty) == TY_ARRAY || L.LLVMGetTypeKind(ty) == TY_VECTOR) {
               ty = L.LLVMGetElementType(ty);
               r.off += (uint64_t)k * air_size(t, ty);
            } else {
               return (struct air_ev) { 0 };
            }
         }
         return r;
      }
      return r;
   }
   LLVMTypeRef ty = L.LLVMTypeOf(v);
   if (L.LLVMIsAUndefValue(v) || L.LLVMGetTypeKind(ty) == TY_VOID)
      return r;
   unsigned size = air_eval_size(t, ty);
   if (size > sizeof r.b || L.LLVMGetTypeKind(ty) == TY_POINTER)
      return r;
   uint8_t known[16];
   r.size = size;
   if ((L.LLVMIsAConstantInt(v) || L.LLVMIsAConstantFP(v) || L.LLVMIsAConstantAggregateZero(v) ||
        L.LLVMIsAConstantDataSequential(v) || L.LLVMIsAConstantVector(v)) &&
       air_bytes_of_constant(t, r.b, known, size, v, ty)) {
      r.known = true;
      for (unsigned i = 0; i < size; i++)
         r.known &= known[i];
   }
   return r;
}

static void
air_eval_set(struct air_eval *e, LLVMValueRef inst, struct air_ev v)
{
   struct air_ev *p = ralloc(e->t->mem, struct air_ev);
   *p = v;
   _mesa_hash_table_insert(e->vals, inst, p);
}

static bool
air_eval_icmp(int pred, uint64_t x, uint64_t y, unsigned bits)
{
   int64_t sx = bits < 64 ? (int64_t)(x << (64 - bits)) >> (64 - bits) : (int64_t)x;
   int64_t sy = bits < 64 ? (int64_t)(y << (64 - bits)) >> (64 - bits) : (int64_t)y;
   switch (pred) {
   case ICMP_EQ: return x == y;
   case ICMP_NE: return x != y;
   case ICMP_UGT: return x > y;
   case ICMP_UGE: return x >= y;
   case ICMP_ULT: return x < y;
   case ICMP_ULE: return x <= y;
   case ICMP_SGT: return sx > sy;
   case ICMP_SGE: return sx >= sy;
   case ICMP_SLT: return sx < sy;
   default: return sx <= sy;
   }
}

/* Floats in the static initializer: half, float and double scalars, read and written as their bits. */
static bool
air_ev_float(const struct air_ev *v, LLVMTypeRef ty, double *d)
{
   switch (L.LLVMGetTypeKind(ty)) {
   case TY_HALF: { uint16_t h; memcpy(&h, v->b, 2); *d = _mesa_half_to_float(h); return true; }
   case TY_FLOAT: { float f; memcpy(&f, v->b, 4); *d = f; return true; }
   case TY_DOUBLE: memcpy(d, v->b, 8); return true;
   default: return false;
   }
}

static bool
air_ev_set_float(struct air_ev *r, LLVMTypeRef ty, double d)
{
   memset(r->b, 0, sizeof r->b);
   switch (L.LLVMGetTypeKind(ty)) {
   case TY_HALF: { uint16_t h = _mesa_float_to_half((float)d); memcpy(r->b, &h, 2); r->size = 2; return true; }
   case TY_FLOAT: { float f = (float)d; memcpy(r->b, &f, 4); r->size = 4; return true; }
   case TY_DOUBLE: memcpy(r->b, &d, 8); r->size = 8; return true;
   default: return false;
   }
}

/* a scalar conversion: to and from as LLVM types, signed for the integer side where it says so */
static bool
air_eval_convert(struct air_tr *t, const struct air_ev *x, LLVMTypeRef from, bool from_signed, LLVMTypeRef to, bool to_signed,
                 struct air_ev *r)
{
   double d;
   bool ffrom = air_ev_float(x, from, &d);
   int kto = L.LLVMGetTypeKind(to);
   if (!ffrom) {
      if (L.LLVMGetTypeKind(from) != TY_INTEGER || L.LLVMGetIntTypeWidth(from) > 64)
         return false;
      unsigned w = L.LLVMGetIntTypeWidth(from);
      uint64_t u = air_ev_u64(x) & BITFIELD64_MASK(w);
      int64_t sv = w < 64 ? (int64_t)(u << (64 - w)) >> (64 - w) : (int64_t)u;
      if (w == 1)
         sv = (int64_t)u;   /* a bool converts as 0 or 1 */
      if (kto == TY_INTEGER) {
         *r = air_ev_int(from_signed ? (uint64_t)sv : u, to);
         return true;
      }
      d = from_signed ? (double)sv : (double)u;
   } else if (kto == TY_INTEGER) {
      unsigned w = L.LLVMGetIntTypeWidth(to);
      if (w == 1) {
         *r = air_ev_int(d != 0.0, to);
         return true;
      }
      if (d != d || d >= 18446744073709551616.0 || d <= -9223372036854775808.0)
         return false;
      *r = air_ev_int(to_signed || d < 0 ? (uint64_t)(int64_t)d : (uint64_t)d, to);
      return true;
   }
   (void)t;
   return air_ev_set_float(r, to, d);
}

/* Runs one instruction of the static initializer. False: this cannot run it (the whole initializer then counts as
 * unknown). */
static bool
air_eval_instruction(struct air_eval *e, LLVMValueRef inst, LLVMBasicBlockRef prev)
{
   struct air_tr *t = e->t;
   int op = L.LLVMGetInstructionOpcode(inst);
   LLVMTypeRef ty = L.LLVMTypeOf(inst);
   struct air_ev r = { 0 };
   switch (op) {
   case OP_LOAD: {
      struct air_ev p = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      unsigned size = air_eval_size(t, ty);
      if (!p.mem || size > sizeof r.b || L.LLVMGetTypeKind(ty) == TY_POINTER || p.off + size > p.mem->size)
         return false;
      r.size = size;
      r.known = true;
      memcpy(r.b, p.mem->bytes + p.off, size);
      for (unsigned i = 0; i < size; i++)
         r.known &= p.mem->known[p.off + i];
      break;
   }
   case OP_STORE: {
      struct air_ev v = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      struct air_ev p = air_eval_value(e, L.LLVMGetOperand(inst, 1));
      unsigned size = air_eval_size(t, L.LLVMTypeOf(L.LLVMGetOperand(inst, 0)));
      if (!p.mem || v.mem || size > sizeof v.b || p.off + size > p.mem->size)
         return false;
      memcpy(p.mem->bytes + p.off, v.b, size);
      memset(p.mem->known + p.off, v.known, size);
      p.mem->stored = true;
      return true;
   }
   case OP_GEP: {
      r = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      if (!r.mem)
         return false;
      LLVMTypeRef sty = L.LLVMGetGEPSourceElementType(inst);
      unsigned n = (unsigned)L.LLVMGetNumOperands(inst);
      for (unsigned i = 1; i < n; i++) {
         struct air_ev k = air_eval_value(e, L.LLVMGetOperand(inst, i));
         if (!k.known || k.mem)
            return false;
         int64_t idx = (int64_t)air_ev_u64(&k);
         unsigned bits = air_width_of(t, L.LLVMGetOperand(inst, i));
         if (bits < 64)
            idx = (idx << (64 - bits)) >> (64 - bits);
         if (i == 1) {
            r.off += (uint64_t)idx * air_size(t, sty);
         } else if (L.LLVMGetTypeKind(sty) == TY_STRUCT) {
            r.off += L.LLVMOffsetOfElement(t->layout, sty, (unsigned)idx);
            sty = L.LLVMStructGetTypeAtIndex(sty, (unsigned)idx);
         } else if (L.LLVMGetTypeKind(sty) == TY_ARRAY || L.LLVMGetTypeKind(sty) == TY_VECTOR) {
            sty = L.LLVMGetElementType(sty);
            r.off += (uint64_t)idx * air_size(t, sty);
         } else {
            return false;
         }
      }
      break;
   }
   case OP_BITCAST: case OP_ADDRSPACECAST: case OP_FREEZE:
      r = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      break;
   case OP_TRUNC: case OP_ZEXT: case OP_SEXT: {
      struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      if (L.LLVMGetTypeKind(ty) != TY_INTEGER || L.LLVMGetIntTypeWidth(ty) > 64 || x.mem)
         return false;
      uint64_t u = air_ev_u64(&x);
      unsigned from = air_width_of(t, L.LLVMGetOperand(inst, 0));
      if (op == OP_SEXT && from < 64)
         u = (uint64_t)((int64_t)(u << (64 - from)) >> (64 - from));
      r = air_ev_int(u, ty);
      r.known = x.known;
      break;
   }
   case OP_FADD: case OP_FSUB: case OP_FMUL: case OP_FDIV: {
      struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0)), y = air_eval_value(e, L.LLVMGetOperand(inst, 1));
      double a, c;
      if (!air_ev_float(&x, ty, &a) || !air_ev_float(&y, ty, &c))
         return false;
      double v = op == OP_FADD ? a + c : op == OP_FSUB ? a - c : op == OP_FMUL ? a * c : a / c;
      if (L.LLVMGetTypeKind(ty) == TY_FLOAT)
         v = (float)v;
      air_ev_set_float(&r, ty, v);
      r.known = x.known && y.known;
      break;
   }
   case OP_FNEG: {
      struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      double a;
      if (!air_ev_float(&x, ty, &a))
         return false;
      air_ev_set_float(&r, ty, -a);
      r.known = x.known;
      break;
   }
   case OP_FCMP: {
      LLVMTypeRef fty = L.LLVMTypeOf(L.LLVMGetOperand(inst, 0));
      struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0)), y = air_eval_value(e, L.LLVMGetOperand(inst, 1));
      double a, c;
      if (!air_ev_float(&x, fty, &a) || !air_ev_float(&y, fty, &c))
         return false;
      bool uno = a != a || c != c, res;
      switch (L.LLVMGetFCmpPredicate(inst)) {
      case FCMP_FALSE: res = false; break;
      case FCMP_OEQ: res = !uno && a == c; break;
      case FCMP_OGT: res = !uno && a > c; break;
      case FCMP_OGE: res = !uno && a >= c; break;
      case FCMP_OLT: res = !uno && a < c; break;
      case FCMP_OLE: res = !uno && a <= c; break;
      case FCMP_ONE: res = !uno && a != c; break;
      case FCMP_ORD: res = !uno; break;
      case FCMP_UNO: res = uno; break;
      case FCMP_UEQ: res = uno || a == c; break;
      case FCMP_UGT: res = uno || a > c; break;
      case FCMP_UGE: res = uno || a >= c; break;
      case FCMP_ULT: res = uno || a < c; break;
      case FCMP_ULE: res = uno || a <= c; break;
      case FCMP_UNE: res = uno || a != c; break;
      default: res = true; break;
      }
      r = air_ev_int(res, ty);
      r.known = x.known && y.known;
      break;
   }
   case OP_UITOFP: case OP_SITOFP: case OP_FPTOUI: case OP_FPTOSI: case OP_FPEXT: case OP_FPTRUNC: {
      LLVMValueRef src = L.LLVMGetOperand(inst, 0);
      struct air_ev x = air_eval_value(e, src);
      if (x.mem || L.LLVMGetTypeKind(L.LLVMTypeOf(src)) == TY_VECTOR ||
          !air_eval_convert(t, &x, L.LLVMTypeOf(src), op == OP_SITOFP, ty, op == OP_FPTOSI, &r))
         return false;
      r.known = x.known;
      break;
   }
   case OP_ICMP: {
      struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0)), y = air_eval_value(e, L.LLVMGetOperand(inst, 1));
      if (x.mem || y.mem || L.LLVMGetTypeKind(L.LLVMTypeOf(L.LLVMGetOperand(inst, 0))) != TY_INTEGER)
         return false;
      r = air_ev_int(air_eval_icmp(L.LLVMGetICmpPredicate(inst), air_ev_u64(&x), air_ev_u64(&y),
                                   air_width_of(t, L.LLVMGetOperand(inst, 0))), ty);
      r.known = x.known && y.known;
      break;
   }
   case OP_SELECT: {
      struct air_ev c = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      if (!c.known || L.LLVMGetTypeKind(L.LLVMTypeOf(L.LLVMGetOperand(inst, 0))) == TY_VECTOR)
         return false;
      r = air_eval_value(e, L.LLVMGetOperand(inst, (c.b[0] & 1) ? 1 : 2));
      break;
   }
   case OP_AND: case OP_OR: case OP_XOR: case OP_ADD: case OP_SUB: case OP_MUL: case OP_SHL: case OP_LSHR: {
      struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0)), y = air_eval_value(e, L.LLVMGetOperand(inst, 1));
      if (x.mem || y.mem || L.LLVMGetTypeKind(ty) != TY_INTEGER || L.LLVMGetIntTypeWidth(ty) > 64)
         return false;
      uint64_t a = air_ev_u64(&x), b = air_ev_u64(&y), u;
      switch (op) {
      case OP_AND: u = a & b; break;
      case OP_OR: u = a | b; break;
      case OP_XOR: u = a ^ b; break;
      case OP_ADD: u = a + b; break;
      case OP_SUB: u = a - b; break;
      case OP_MUL: u = a * b; break;
      case OP_SHL: u = b < 64 ? a << b : 0; break;
      default: u = b < 64 ? a >> b : 0; break;
      }
      r = air_ev_int(u, ty);
      r.known = x.known && y.known;
      break;
   }
   case OP_EXTRACTELEMENT: case OP_INSERTELEMENT: {
      LLVMTypeRef vty = L.LLVMTypeOf(L.LLVMGetOperand(inst, 0));
      LLVMValueRef index = L.LLVMGetOperand(inst, op == OP_EXTRACTELEMENT ? 1 : 2);
      if (L.LLVMGetTypeKind(vty) != TY_VECTOR || !L.LLVMIsAConstantInt(index))
         return false;
      unsigned es = air_eval_size(t, L.LLVMGetElementType(vty)), k = (unsigned)L.LLVMConstIntGetZExtValue(index);
      struct air_ev v = air_eval_value(e, L.LLVMGetOperand(inst, 0));
      if (v.mem || (k + 1) * es > sizeof v.b)
         return false;
      if (op == OP_EXTRACTELEMENT) {
         r.size = es;
         r.known = v.known;
         memcpy(r.b, v.b + k * es, es);
      } else {
         struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 1));
         r = v;
         r.size = air_eval_size(t, vty);
         memcpy(r.b + k * es, x.b, es);
         r.known = (v.known || L.LLVMIsAUndefValue(L.LLVMGetOperand(inst, 0))) && x.known;
      }
      break;
   }
   case OP_PHI: {
      unsigned n = L.LLVMCountIncoming(inst);
      bool found = false;
      for (unsigned i = 0; i < n && !found; i++) {
         if (L.LLVMGetIncomingBlock(inst, i) == prev) {
            r = air_eval_value(e, L.LLVMGetIncomingValue(inst, i));
            found = true;
         }
      }
      if (!found)
         return false;
      break;
   }
   case OP_CALL: {
      LLVMValueRef callee = L.LLVMGetCalledValue(inst);
      const char *name = callee && L.LLVMIsAFunction(callee) ? air_name(callee, NULL) : "";
      if (air_starts(name, "llvm.lifetime.") || air_starts(name, "llvm.dbg.") || air_starts(name, "llvm.assume"))
         return true;
      if (air_starts(name, "air.convert.") && L.LLVMGetTypeKind(ty) != TY_VECTOR && L.LLVMGetNumArgOperands(inst) == 1) {
         /* air.convert.<s|u|f>.<to>.<s|u|f>.<from> */
         const char *p = name + strlen("air.convert.");
         LLVMValueRef src = L.LLVMGetOperand(inst, 0);
         const char *from = strchr(p, '.');
         from = from ? strchr(from + 1, '.') : NULL;
         struct air_ev x = air_eval_value(e, src);
         if (!from || x.mem || L.LLVMGetTypeKind(L.LLVMTypeOf(src)) == TY_VECTOR ||
             !air_eval_convert(t, &x, L.LLVMTypeOf(src), from[1] == 's', ty, p[0] == 's', &r))
            return false;
         r.known = x.known;
         break;
      }
      if (air_starts(name, "air.normalize_function_constant_predicate.") && L.LLVMGetTypeKind(ty) == TY_INTEGER) {
         struct air_ev x = air_eval_value(e, L.LLVMGetOperand(inst, 0));
         r = air_ev_int(air_ev_u64(&x) != 0, ty);
         r.known = x.known;
         break;
      }
      if (air_starts(name, "llvm.memcpy.") || air_starts(name, "llvm.memmove.")) {
         struct air_ev d = air_eval_value(e, L.LLVMGetOperand(inst, 0)), s = air_eval_value(e, L.LLVMGetOperand(inst, 1));
         LLVMValueRef len = L.LLVMGetOperand(inst, 2);
         if (!d.mem || !s.mem || !L.LLVMIsAConstantInt(len))
            return false;
         uint64_t n = L.LLVMConstIntGetZExtValue(len);
         if (d.off + n > d.mem->size || s.off + n > s.mem->size)
            return false;
         memmove(d.mem->bytes + d.off, s.mem->bytes + s.off, n);
         memmove(d.mem->known + d.off, s.mem->known + s.off, n);
         d.mem->stored = true;
         return true;
      }
      return false;
   }
   default:
      return false;
   }
   air_eval_set(e, inst, r);
   return true;
}

/* The globals a static initializer stores to, found without running it. */
static void
air_eval_mark_stored(struct air_eval *e, LLVMValueRef fn)
{
   for (LLVMBasicBlockRef bb = L.LLVMGetFirstBasicBlock(fn); bb; bb = L.LLVMGetNextBasicBlock(bb)) {
      for (LLVMValueRef inst = L.LLVMGetFirstInstruction(bb); inst; inst = L.LLVMGetNextInstruction(inst)) {
         int op = L.LLVMGetInstructionOpcode(inst);
         LLVMValueRef ptr = op == OP_STORE ? L.LLVMGetOperand(inst, 1) : NULL;
         if (op == OP_CALL) {
            LLVMValueRef callee = L.LLVMGetCalledValue(inst);
            const char *name = callee && L.LLVMIsAFunction(callee) ? air_name(callee, NULL) : "";
            if (air_starts(name, "llvm.memcpy.") || air_starts(name, "llvm.memmove.") || air_starts(name, "llvm.memset."))
               ptr = L.LLVMGetOperand(inst, 0);
         }
         /* down to the global: GEPs, casts */
         for (unsigned k = 0; ptr && k < 16 && !L.LLVMIsAGlobalVariable(ptr); k++) {
            if (L.LLVMIsAConstantExpr(ptr) || L.LLVMGetInstructionOpcode(ptr) == OP_GEP ||
                L.LLVMGetInstructionOpcode(ptr) == OP_BITCAST || L.LLVMGetInstructionOpcode(ptr) == OP_ADDRSPACECAST)
               ptr = L.LLVMGetOperand(ptr, 0);
            else
               ptr = NULL;
         }
         if (ptr && L.LLVMIsAGlobalVariable(ptr)) {
            struct air_mem *m = air_eval_mem(e, ptr);
            m->stored = true;
            memset(m->known, 0, m->size);
         }
      }
   }
}

static void
air_eval_static_init(struct air_eval *e, LLVMValueRef fn)
{
   LLVMBasicBlockRef bb = L.LLVMGetEntryBasicBlock(fn), prev = NULL;
   while (bb) {
      LLVMBasicBlockRef next = NULL;
      for (LLVMValueRef inst = L.LLVMGetFirstInstruction(bb); inst; inst = L.LLVMGetNextInstruction(inst)) {
         if (++e->steps > 200000)
            goto unknown;
         int op = L.LLVMGetInstructionOpcode(inst);
         if (op == OP_RET)
            return;
         if (op == OP_SWITCH) {
            e->t->rf_init_inexact = true;
            struct air_ev c = air_eval_value(e, L.LLVMGetOperand(inst, 0));
            if (!c.known || c.mem)
               goto unknown;
            next = L.LLVMGetSuccessor(inst, 0);
            unsigned cases = L.LLVMGetNumSuccessors(inst);
            for (unsigned k = 1; k < cases; k++) {
               LLVMValueRef v = L.LLVMGetOperand(inst, 2 * k);
               uint64_t mask = BITFIELD64_MASK(MIN2(64, air_width_of(e->t, v)));
               if (L.LLVMIsAConstantInt(v) && ((L.LLVMConstIntGetZExtValue(v) ^ air_ev_u64(&c)) & mask) == 0) {
                  next = L.LLVMGetSuccessor(inst, k);
                  break;
               }
            }
            break;
         }
         if (op == OP_BR) {
            if (L.LLVMIsConditional(inst)) {
               e->t->rf_init_inexact = true;
               struct air_ev c = air_eval_value(e, L.LLVMGetCondition(inst));
               if (!c.known)
                  goto unknown;
               next = L.LLVMGetSuccessor(inst, (c.b[0] & 1) ? 0 : 1);
            } else {
               next = L.LLVMGetSuccessor(inst, 0);
            }
            break;
         }
         if (!air_eval_instruction(e, inst, prev)) {
            if (getenv("NVMTL_AIR_NIR_DEBUG")) {
               LLVMValueRef callee = op == OP_CALL ? L.LLVMGetCalledValue(inst) : NULL;
               fprintf(stderr, "air-nir: static initializer %s stops at LLVM opcode %d %s\n", air_name(fn, NULL), op,
                       callee ? air_name(callee, NULL) : air_name(inst, NULL));
            }
            goto unknown;
         }
      }
      if (!next)
         goto unknown;
      prev = bb;
      bb = next;
   }
unknown:
   /* what this initializer would have left is not known: every global it stores to is unknown */
   air_eval_mark_stored(e, fn);
}

/* Applies the request's function-constant values to the module before LLVM's passes; true when the module has function
 * constants (and the passes that fold what they decide should run). */
static bool
air_fc_specialize(struct air_tr *t, LLVMContextRef ctx)
{
   if (!air_has_fc_values(t))
      return false;
   for (uint32_t i = 0; i < t->req->fc_count; i++) {
      for (uint32_t j = 0; j < i; j++) {
         if (t->req->fc_indices[j] == t->req->fc_indices[i])
            air_refuse(t, NVMTL_AIR_NIR_BAD_REQUEST, "function constant %u is given twice", t->req->fc_indices[i]);
      }
      if (!t->req->fc_payloads[i] && t->req->fc_sizes[i])
         air_refuse(t, NVMTL_AIR_NIR_BAD_REQUEST, "function constant %u has no payload", t->req->fc_indices[i]);
   }
   t->layout = L.LLVMGetModuleDataLayout(t->module);
   bool any = false;
   for (LLVMValueRef g = L.LLVMGetFirstGlobal(t->module); g; g = L.LLVMGetNextGlobal(g)) {
      uint32_t index, size = 0;
      if (!air_fc_index(g, &index))
         continue;
      any = true;
      const uint8_t *payload = air_fc_given(t, index, &size);
      if (!payload)
         continue;
      LLVMTypeRef vty = L.LLVMGlobalGetValueType(g);
      uint64_t want = L.LLVMStoreSizeOfType(t->layout, vty);
      if (size != want)
         air_refuse(t, NVMTL_AIR_NIR_BAD_REQUEST, "function constant %u has %u bytes, its type %llu", index, size,
                    (unsigned long long)want);
      if (air_fc_bool_abi(g) && payload[0] > 1)
         air_refuse(t, NVMTL_AIR_NIR_BAD_REQUEST, "function constant %u is a bool, given %u", index, payload[0]);
      uint8_t bytes[64] = { 0 };
      if (size > sizeof bytes)
         unsupported(t, "function constant %u of %u bytes", index, size);
      memcpy(bytes, payload, size);
      LLVMValueRef c = air_constant_from_bytes(t, ctx, vty, bytes);
      if (!c)
         unsupported(t, "function constant %u of a type this cannot build (%s)", index, air_name(g, NULL));
      L.LLVMSetInitializer(g, c);
      L.LLVMSetExternallyInitialized(g, 0);
      L.LLVMSetGlobalConstant(g, 1);
   }
   if (!any)
      return false;

   /* air.is_function_constant_defined(@x.MTL_FC_INIT_n): whether the request gives n */
   LLVMValueRef defined = L.LLVMGetNamedFunction(t->module, "air.is_function_constant_defined");
   if (defined) {
      struct util_dynarray calls;
      util_dynarray_init(&calls, t->mem);
      for (LLVMUseRef u = L.LLVMGetFirstUse(defined); u; u = L.LLVMGetNextUse(u)) {
         LLVMValueRef call = L.LLVMGetUser(u);
         if (L.LLVMGetInstructionOpcode(call) == OP_CALL && L.LLVMGetCalledValue(call) == defined)
            util_dynarray_append(&calls, call);
      }
      util_dynarray_foreach(&calls, LLVMValueRef, pc) {
         LLVMValueRef g = L.LLVMGetOperand(*pc, 0);
         while (L.LLVMIsAConstantExpr(g))
            g = L.LLVMGetOperand(g, 0);
         uint32_t index, size;
         if (!L.LLVMIsAGlobalVariable(g) || !air_fc_index(g, &index))
            unsupported(t, "air.is_function_constant_defined of something that is not a function constant");
         const bool given = air_fc_given(t, index, &size) != NULL;
         t->rf_init_inexact |= given;
         L.LLVMReplaceAllUsesWith(*pc, L.LLVMConstInt(L.LLVMInt1TypeInContext(ctx), given, 0));
         L.LLVMInstructionEraseFromParent(*pc);
      }
   }

   /* run the static initializers; every global they fill with known bytes becomes that constant */
   struct air_eval e = { .t = t };
   e.mems = _mesa_pointer_hash_table_create(t->mem);
   e.vals = _mesa_pointer_hash_table_create(t->mem);
   /* in @llvm.global_ctors' order (by priority, then as listed), as the static initializers run; then any other
    * function of the section */
   struct set *done = _mesa_pointer_set_create(t->mem);
   LLVMValueRef ctors = NULL;
   for (LLVMValueRef g = L.LLVMGetFirstGlobal(t->module); g && !ctors; g = L.LLVMGetNextGlobal(g)) {
      if (!strcmp(air_name(g, NULL), "llvm.global_ctors"))
         ctors = L.LLVMGetInitializer(g);
   }
   unsigned nctors = ctors && L.LLVMGetTypeKind(L.LLVMTypeOf(ctors)) == TY_ARRAY ?
                     (unsigned)L.LLVMGetArrayLength2(L.LLVMTypeOf(ctors)) : 0;
   /* the lowest priority first, then as listed: one scan per priority */
   uint64_t prio = 0;
   for (unsigned round = 0; round <= nctors; round++) {
      uint64_t next = ~0ull;
      for (unsigned i = 0; i < nctors; i++) {
         LLVMValueRef c = L.LLVMGetAggregateElement(ctors, i);
         LLVMValueRef p = c ? L.LLVMGetAggregateElement(c, 0) : NULL, f = c ? L.LLVMGetAggregateElement(c, 1) : NULL;
         while (f && L.LLVMIsAConstantExpr(f))
            f = L.LLVMGetOperand(f, 0);
         if (!p || !f || !L.LLVMIsAConstantInt(p) || !L.LLVMIsAFunction(f))
            continue;
         uint64_t pr = L.LLVMConstIntGetZExtValue(p);
         if (pr == prio && !_mesa_set_search(done, f) && !L.LLVMIsDeclaration(f)) {
            _mesa_set_add(done, f);
            air_eval_static_init(&e, f);
         } else if (pr > prio && pr < next) {
            next = pr;
         }
      }
      if (next == ~0ull)
         break;
      prio = next;
   }
   for (LLVMValueRef f = L.LLVMGetFirstFunction(t->module); f; f = L.LLVMGetNextFunction(f)) {
      const char *section = L.LLVMGetSection(f);
      if (!L.LLVMIsDeclaration(f) && section && !strcmp(section, "air.static_init") && !_mesa_set_search(done, f)) {
         _mesa_set_add(done, f);
         air_eval_static_init(&e, f);
      }
   }
   hash_table_foreach(e.mems, he) {
      struct air_mem *m = he->data;
      if (!m->stored)
         continue;
      bool known = true;
      for (uint64_t i = 0; i < m->size && known; i++)
         known = m->known[i];
      if (!known)
         continue;
      LLVMValueRef c = air_constant_from_bytes(t, ctx, L.LLVMGlobalGetValueType(m->global), m->bytes);
      if (!c)
         continue;
      L.LLVMSetInitializer(m->global, c);
      L.LLVMSetExternallyInitialized(m->global, 0);
      L.LLVMSetGlobalConstant(m->global, 1);
   }
   return true;
}

/* Every function the kernel may call is inlined: Apple's compiler builds with optsize, and the inliner's threshold
 * for it leaves calls (Espresso's and MPS's helpers among them) that this frontend cannot translate. */
static void
air_mark_inline(struct air_tr *t, LLVMContextRef ctx)
{
   const unsigned always = L.LLVMGetEnumAttributeKindForName("alwaysinline", 12);
   const unsigned never = L.LLVMGetEnumAttributeKindForName("noinline", 8);
   const unsigned optnone = L.LLVMGetEnumAttributeKindForName("optnone", 7);
   if (!always)
      return;
   LLVMAttributeRef attr = L.LLVMCreateEnumAttribute(ctx, always, 0);
   for (LLVMValueRef f = L.LLVMGetFirstFunction(t->module); f; f = L.LLVMGetNextFunction(f)) {
      if (L.LLVMIsDeclaration(f))   /* a kernel is called by nothing: marking it changes nothing */
         continue;
      if (never)
         L.LLVMRemoveEnumAttributeAtIndex(f, ~0u /* the function */, never);
      if (optnone)
         L.LLVMRemoveEnumAttributeAtIndex(f, ~0u, optnone);
      L.LLVMAddAttributeAtIndex(f, ~0u, attr);
   }
}

static void
air_run_passes(struct air_tr *t, LLVMContextRef ctx, bool fold)
{
   air_mark_inline(t, ctx);
   LLVMPassBuilderOptionsRef opts = L.LLVMCreatePassBuilderOptions();
   /* with function constants fixed, the branches they decide are folded before the kernel is read, so a value the
    * request did not give is refused only where a live path still reads it */
   LLVMErrorRef err = L.LLVMRunPasses(t->module, fold ? "always-inline,inline,function(sroa,sccp,instsimplify,simplifycfg,adce)"
                                                      : "always-inline,inline,function(sroa)", NULL, opts);
   L.LLVMDisposePassBuilderOptions(opts);
   /* NVMTL_AIR_NIR_DUMP=<file>: the module as the kernel is read from it, for looking into a refusal */
   const char *dump = getenv("NVMTL_AIR_NIR_DUMP");
   if (dump && *dump && !err) {
      FILE *f = fopen(dump, "w");
      char *text = f ? L.LLVMPrintModuleToString(t->module) : NULL;
      if (text) {
         fputs(text, f);
         L.LLVMDisposeMessage(text);
      }
      if (f)
         fclose(f);
   }
   if (err) {
      char *m = L.LLVMGetErrorMessage(err);
      snprintf(t->llvm_error, sizeof t->llvm_error, "%s", m ? m : "?");
      L.LLVMDisposeErrorMessage(m);
      air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "LLVM's inliner refused the module: %s", t->llvm_error);
   }
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Reflection (version 5, docs/AIR-TO-NIR.md phase 4)                                                                 */
/*                                                                                                                    */
/* With NVMTL_AIR_NIR_REFLECT the result also carries, as JSON, what the plugin reads from the translator's           */
/* reflection (nvmtl_reflect / nvmtl_reflect_fc: translator/translator/src/reflect and meta), with the translator's   */
/* keys and values: the bindings (kind, metal_index, descriptor, param_index, address_space, declared_size, extent,  */
/* type_layout, type_name, texture_shape, access, static_sampler), the varyings, the vertex attributes, the kernel's  */
/* default threadgroup size (local_size) and its max_work_group_size, and the keys a function this frontend          */
/* translates never has (tessellation, fragment_sample_positions, argument_buffer_fields, null or empty). It is read */
/* from the module as the translator's function-constant specialization leaves it (an argument a constant that is    */
/* known to be true keeps, one it knows to be false drops, one it cannot decide stays gated), by the translator's     */
/* rules for each stage, from the same metadata: so the plugin can take it instead of the translator's for a function */
/* this frontend translates. The keys the plugin never reads (footprints, descriptor_layout, render_targets, the     */
/* function constants, ...) are not written. A module whose reflection would need what this does not model gives    */
/* none (reflection NULL and reflection_why), and the plugin asks the translator.                                     */

#define AIR_RF_USED      1u   /* the parameter has a use in the entry (the translator: its name occurs in the body) */
#define AIR_RF_READNONE  2u
#define AIR_RF_READONLY  4u
#define AIR_RF_WRITEONLY 8u

struct air_rf_node {
   LLVMValueRef *ops;
   unsigned n;
};

static struct air_rf_node
air_rf_node(struct air_tr *t, LLVMValueRef node)
{
   struct air_rf_node x = { NULL, 0 };
   if (node && L.LLVMIsAMDNode(node))
      x.n = air_md_operands(node, &x.ops, t->mem);
   return x;
}

/* the index of the first operand that is the string marker, or -1 */
static int
air_rf_find(const struct air_rf_node *x, const char *marker)
{
   for (unsigned i = 0; i < x->n; i++) {
      const char *s = air_md_string(x->ops[i]);
      if (s && !strcmp(s, marker))
         return (int)i;
   }
   return -1;
}

/* an operand the translator's text reads as "i32 N", N a u32 */
static bool
air_rf_i32(LLVMValueRef v, uint32_t *out)
{
   if (!v || !L.LLVMIsAConstantInt(v) || L.LLVMGetIntTypeWidth(L.LLVMTypeOf(v)) != 32)
      return false;
   long long s = L.LLVMConstIntGetSExtValue(v);
   if (s < 0)
      return false;
   *out = (uint32_t)s;
   return true;
}

/* meta::i32_after_marker: the first "i32 N" after the marker */
static bool
air_rf_i32_after(const struct air_rf_node *x, const char *marker, uint32_t *out)
{
   int k = air_rf_find(x, marker);
   for (unsigned i = k < 0 ? x->n : (unsigned)k + 1; i < x->n; i++) {
      if (air_rf_i32(x->ops[i], out))
         return true;
   }
   return false;
}

/* meta::string_after_marker: the next string after the marker */
static const char *
air_rf_string_after(const struct air_rf_node *x, const char *marker)
{
   int k = air_rf_find(x, marker);
   for (unsigned i = k < 0 ? x->n : (unsigned)k + 1; i < x->n; i++) {
      const char *s = air_md_string(x->ops[i]);
      if (s)
         return s;
   }
   return NULL;
}

/* meta::declared_buffer_access: the node's text holds air.read_write, air.write or air.read */
static const char *
air_rf_declared_access(const struct air_rf_node *x)
{
   bool rw = false, w = false, r = false;
   for (unsigned i = 0; i < x->n; i++) {
      const char *s = air_md_string(x->ops[i]);
      if (!s)
         continue;
      rw |= strstr(s, "air.read_write") != NULL;
      w |= strstr(s, "air.write") != NULL;
      r |= strstr(s, "air.read") != NULL;
   }
   return rw ? "ReadWrite" : w ? "WriteOnly" : r ? "ReadOnly" : NULL;
}

/* The value of the global a function-constant gate {global, ...} names, as the translator's static initializer
 * evaluation knows it: 1 or 0, -1 when not known */
static int
air_rf_gate_value(struct air_tr *t, LLVMValueRef gate)
{
   struct air_rf_node g = air_rf_node(t, gate);
   for (unsigned i = 0; i < g.n; i++) {
      LLVMValueRef v = g.ops[i];
      while (v && L.LLVMIsAConstantExpr(v))
         v = L.LLVMGetOperand(v, 0);
      if (!v || !L.LLVMIsAGlobalVariable(v))
         continue;
      if (!L.LLVMIsGlobalConstant(v) || L.LLVMIsExternallyInitialized(v))
         return -1;
      LLVMValueRef init = L.LLVMGetInitializer(v);
      if (!init || !L.LLVMIsAConstantInt(init))
         return -1;
      /* a predicate Apple's compiler computes from constants ([[function_constant(a && b)]], ...): LLVM ran the
       * initializer; where the translator's text evaluation of it may differ (a constant is_function_constant_defined
       * finds given, a branch), this reflection is not given (the plugin asks the translator) */
      uint32_t index;
      if (!air_fc_index(v, &index) && t->rf_init_inexact)
         t->rf_computed_gate = true;
      return L.LLVMConstIntGetZExtValue(init) != 0;
   }
   return -1;
}

/* The node's role strings (the air.* strings, without "air.") as the translator's function-constant specialization
 * leaves them (meta::specialize_function_constant_metadata): a gate known true is taken out, one known false makes
 * the first "function_constant_disabled". *gated: the first is still "function_constant" (a gate not known). */
static unsigned
air_rf_roles(struct air_tr *t, const struct air_rf_node *x, const char **roles, unsigned max)
{
   unsigned n = 0;
   int first_at = -1;
   for (unsigned i = 0; i < x->n && n < max; i++) {
      const char *s = air_md_string(x->ops[i]);
      if (s && !strncmp(s, "air.", 4)) {
         if (!n)
            first_at = (int)i;
         roles[n++] = s + 4;
      }
   }
   if (n && !strcmp(roles[0], "function_constant") && t->rf_rewrite) {
      int v = first_at >= 0 && (unsigned)first_at + 1 < x->n ? air_rf_gate_value(t, x->ops[first_at + 1]) : -1;
      if (v == 1) {
         memmove(roles, roles + 1, (n - 1) * sizeof *roles);
         n--;
      } else if (v == 0) {
         roles[0] = "function_constant_disabled";
      }
   }
   return n;
}

/* meta::primary_role: the first role that is not "function_constant" */
static const char *
air_rf_primary(const char **roles, unsigned n)
{
   for (unsigned i = 0; i < n; i++) {
      if (strcmp(roles[i], "function_constant"))
         return roles[i];
   }
   return NULL;
}

/* the first operand after air.location_index (the slot) and the one after it (an array's count); a global is the
 * value the static initializer left in it. 1: a literal or a known global, 0: none, -1: a global not known */
static int
air_rf_location(struct air_tr *t, const struct air_rf_node *x, uint32_t *slot, uint32_t *count)
{
   int k = air_rf_find(x, "air.location_index");
   *count = 0;
   if (k < 0 || (unsigned)k + 1 >= x->n)
      return 0;
   if ((unsigned)k + 2 < x->n)
      air_rf_i32(x->ops[k + 2], count);
   if (air_rf_i32(x->ops[k + 1], slot))
      return 1;
   LLVMValueRef g = x->ops[k + 1];
   while (g && L.LLVMIsAConstantExpr(g))
      g = L.LLVMGetOperand(g, 0);
   if (!g || !L.LLVMIsAGlobalVariable(g))
      return 0;
   LLVMValueRef init = L.LLVMIsGlobalConstant(g) && !L.LLVMIsExternallyInitialized(g) ? L.LLVMGetInitializer(g) : NULL;
   if (!init || !L.LLVMIsAConstantInt(init))
      return -1;
   *slot = (uint32_t)L.LLVMConstIntGetZExtValue(init);
   return 1;
}

/* meta::declared_slot for air.location_index: the slot, or the fallback */
static uint32_t
air_rf_slot(struct air_tr *t, const struct air_rf_node *x, uint32_t fallback, bool *global)
{
   uint32_t slot = fallback, count;
   int k = air_rf_find(x, "air.location_index");
   *global = k >= 0 && (unsigned)k + 1 < x->n && !L.LLVMIsAConstantInt(x->ops[k + 1]);
   if (air_rf_location(t, x, &slot, &count) <= 0)
      slot = fallback;
   return slot;
}

/* ---- JSON ---- */

static void
air_rf_jstr(char **j, const char *s)
{
   if (!s) {
      ralloc_strcat(j, "null");
      return;
   }
   ralloc_strcat(j, "\"");
   for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
      if (*p == '"' || *p == '\\')
         ralloc_asprintf_append(j, "\\%c", *p);
      else if (*p < 0x20)
         ralloc_asprintf_append(j, "\\u%04x", *p);
      else
         ralloc_asprintf_append(j, "%c", *p);
   }
   ralloc_strcat(j, "\"");
}

static void
air_rf_jopt(char **j, bool have, uint32_t v)
{
   if (have)
      ralloc_asprintf_append(j, "%u", v);
   else
      ralloc_strcat(j, "null");
}

/* ---- types (meta::types): a buffer's type_layout from its air.struct_type_info ---- */

static const char *const air_rf_scalars[][2] = {
   { "ulong", "ULong" }, { "long", "SLong" }, { "ushort", "UShort" }, { "short", "SShort" }, { "uchar", "UChar" },
   { "char", "UChar" }, { "float", "Float" }, { "half", "Half" }, { "uint", "UInt" }, { "int", "SInt" },
   { "bool", "Bool" },
};

/* meta::primitive_air_type_from_name as JSON into *j; false when the name is no primitive */
static bool
air_rf_primitive(char **j, const char *name)
{
   while (*name == ' ' || *name == '\t' || *name == '\n')
      name++;
   size_t len = strlen(name);
   while (len && (name[len - 1] == ' ' || name[len - 1] == '\t' || name[len - 1] == '\n'))
      len--;
   char raw[128];
   if (len >= sizeof raw)
      return false;
   memcpy(raw, name, len);
   raw[len] = 0;
   const char *n = raw;
   bool packed = false;
   if (!strncmp(n, "packed_", 7)) {
      n += 7;
      packed = true;
   }
   for (unsigned i = 0; i < ARRAY_SIZE(air_rf_scalars); i++) {
      size_t b = strlen(air_rf_scalars[i][0]);
      if (strncmp(n, air_rf_scalars[i][0], b))
         continue;
      const char *rest = n + b, *scalar = air_rf_scalars[i][1];
      if (!*rest) {
         ralloc_asprintf_append(j, "{\"Scalar\":\"%s\"}", scalar);
         return true;
      }
      const char *x = strchr(rest, 'x');
      if (x) {
         /* "CxR": both parse as u32 after trimming */
         char a[32], c[32];
         size_t la = (size_t)(x - rest);
         if (la >= sizeof a || strlen(x + 1) >= sizeof c)
            return false;
         memcpy(a, rest, la);
         a[la] = 0;
         strcpy(c, x + 1);
         char *e1, *e2;
         unsigned long cols = strtoul(a, &e1, 10), rows = strtoul(c, &e2, 10);
         if (e1 == a || *e1 || e2 == c || *e2 || a[0] == '-' || a[0] == '+' || c[0] == '-' || c[0] == '+')
            return false;
         ralloc_asprintf_append(j, "{\"Matrix\":{\"scalar\":\"%s\",\"cols\":%lu,\"rows\":%lu}}", scalar, cols, rows);
         return true;
      }
      char *e;
      if (*rest < '0' || *rest > '9')
         return false;
      unsigned long lanes = strtoul(rest, &e, 10);
      if (*e)
         return false;
      if (lanes >= 1 && packed)
         ralloc_asprintf_append(j, "{\"PackedVec\":{\"scalar\":\"%s\",\"lanes\":%lu}}", scalar, lanes);
      else if (lanes >= 2)
         ralloc_asprintf_append(j, "{\"Vec\":{\"scalar\":\"%s\",\"lanes\":%lu}}", scalar, lanes);
      else
         return false;
      return true;
   }
   return false;
}

/* meta::storage_air_type_for_size */
static void
air_rf_storage(char **j, uint32_t size)
{
   switch (size) {
   case 0:
   case 4: ralloc_strcat(j, "{\"Scalar\":\"UInt\"}"); return;
   case 1: ralloc_strcat(j, "{\"Scalar\":\"UChar\"}"); return;
   case 2: ralloc_strcat(j, "{\"Scalar\":\"UShort\"}"); return;
   case 8: ralloc_strcat(j, "{\"Scalar\":\"ULong\"}"); return;
   default:
      if (size % 4 == 0)
         ralloc_asprintf_append(j, "{\"Array\":{\"elem\":{\"Scalar\":\"UInt\"},\"len\":%u}}", size / 4);
      else
         ralloc_asprintf_append(j, "{\"Array\":{\"elem\":{\"Scalar\":\"UChar\"},\"len\":%u}}", size);
   }
}

/* meta::types::tokenize: an i32 that is a u32 is an Int, a string a Str (as far as its first comma: the translator
 * splits the node's text at every comma), a node a Ref; anything else is no token */
enum air_rf_tok_kind { AIR_RF_INT, AIR_RF_STR, AIR_RF_REF };
struct air_rf_tok {
   enum air_rf_tok_kind kind;
   uint32_t i;
   const char *s;
   LLVMValueRef ref;
};

static unsigned
air_rf_tokens(struct air_tr *t, LLVMValueRef node, struct air_rf_tok **out)
{
   struct air_rf_node x = air_rf_node(t, node);
   struct air_rf_tok *tk = rzalloc_array(t->mem, struct air_rf_tok, x.n + 1);
   unsigned n = 0;
   for (unsigned i = 0; i < x.n; i++) {
      LLVMValueRef v = x.ops[i];
      const char *s = air_md_string(v);
      if (s) {
         const char *c = strchr(s, ',');
         tk[n].kind = AIR_RF_STR;
         tk[n].s = c ? ralloc_strndup(t->mem, s, (size_t)(c - s)) : s;
         n++;
      } else if (v && L.LLVMIsAMDNode(v)) {
         tk[n].kind = AIR_RF_REF;
         tk[n].ref = v;
         n++;
      } else if (air_rf_i32(v, &tk[n].i)) {
         tk[n].kind = AIR_RF_INT;
         n++;
      }
   }
   *out = tk;
   return n;
}

static bool
air_rf_tok_is(const struct air_rf_tok *tk, unsigned n, unsigned i, enum air_rf_tok_kind k)
{
   return i < n && tk[i].kind == k;
}

/* meta::types::struct_member_starts_at */
static bool
air_rf_member_starts(const struct air_rf_tok *tk, unsigned n, unsigned i)
{
   if (air_rf_tok_is(tk, n, i, AIR_RF_STR) && !strcmp(tk[i].s, "air.struct_type_info") && air_rf_tok_is(tk, n, i + 1, AIR_RF_REF))
      i += 2;
   return air_rf_tok_is(tk, n, i, AIR_RF_INT) && air_rf_tok_is(tk, n, i + 1, AIR_RF_INT) &&
          air_rf_tok_is(tk, n, i + 2, AIR_RF_INT) && air_rf_tok_is(tk, n, i + 3, AIR_RF_STR) &&
          air_rf_tok_is(tk, n, i + 4, AIR_RF_STR);
}

/* whether a node's role strings name a resource other than an indirect constant (meta::types::member_holds_resource_handle) */
static bool
air_rf_resource_handle(struct air_tr *t, LLVMValueRef node)
{
   if (!node)
      return false;
   struct air_rf_node x = air_rf_node(t, node);
   const char *roles[64];
   unsigned n = 0;
   for (unsigned i = 0; i < x.n && n < 64; i++) {
      const char *s = air_md_string(x.ops[i]);
      if (s && !strncmp(s, "air.", 4))
         roles[n++] = s + 4;
   }
   const char *p = air_rf_primary(roles, n);
   return p && strcmp(p, "indirect_constant");
}

/* meta::types::parse_struct_info as JSON into *j; false (nothing written) when the translator's gives None */
static bool
air_rf_struct(struct air_tr *t, char **j, LLVMValueRef node, unsigned depth)
{
   if (depth > 16 || !node || !L.LLVMIsAMDNode(node))
      return false;
   struct air_rf_tok *tk;
   const unsigned n = air_rf_tokens(t, node, &tk);
   struct air_rf_member { uint32_t offset, size, len; const char *name; LLVMValueRef nested, arg; } *m =
      rzalloc_array(t->mem, struct air_rf_member, n + 1);
   unsigned count = 0;
   for (unsigned i = 0; i < n;) {
      LLVMValueRef nested = NULL;
      if (air_rf_tok_is(tk, n, i, AIR_RF_STR) && !strcmp(tk[i].s, "air.struct_type_info") && air_rf_tok_is(tk, n, i + 1, AIR_RF_REF)) {
         nested = tk[i + 1].ref;
         i += 2;
      }
      if (!air_rf_tok_is(tk, n, i, AIR_RF_INT) || !air_rf_tok_is(tk, n, i + 1, AIR_RF_INT) ||
          !air_rf_tok_is(tk, n, i + 2, AIR_RF_INT) || !air_rf_tok_is(tk, n, i + 3, AIR_RF_STR))
         break;
      struct air_rf_member *e = &m[count++];
      e->offset = tk[i].i;
      e->size = tk[i + 1].i;
      e->len = tk[i + 2].i;
      e->name = tk[i + 3].s;
      e->nested = nested;
      i += 5;
      while (i < n && !air_rf_member_starts(tk, n, i)) {
         if (air_rf_tok_is(tk, n, i, AIR_RF_STR) && !strcmp(tk[i].s, "air.indirect_argument") && air_rf_tok_is(tk, n, i + 1, AIR_RF_REF))
            e->arg = tk[i + 1].ref;
         i++;
      }
   }
   if (!count)
      return false;
   for (unsigned k = 0; k + 1 < count; k++) {
      if ((uint64_t)m[k].offset + (uint64_t)m[k].size * MAX2(m[k].len, 1u) > m[k + 1].offset)
         return false;
   }
   ralloc_strcat(j, "{\"Struct\":[");
   for (unsigned k = 0; k < count; k++) {
      ralloc_asprintf_append(j, "%s{\"offset\":%u,\"ty\":", k ? "," : "", m[k].offset);
      if (m[k].len > 0)
         ralloc_strcat(j, "{\"Array\":{\"elem\":");
      char *sub = ralloc_strdup(t->mem, "");
      if (m[k].nested) {
         if (!air_rf_struct(t, &sub, m[k].nested, depth + 1)) {
            sub[0] = 0;
            air_rf_storage(&sub, m[k].size);
         }
      } else if (air_rf_resource_handle(t, m[k].arg) || !air_rf_primitive(&sub, m[k].name)) {
         if (air_rf_resource_handle(t, m[k].arg))
            air_rf_storage(&sub, m[k].size);
         else
            ralloc_asprintf_append(&sub, "{\"Opaque\":{\"size\":%u}}", m[k].size);
      }
      ralloc_strcat(j, sub);
      if (m[k].len > 0)
         ralloc_asprintf_append(j, ",\"len\":%u}}", m[k].len);
      ralloc_strcat(j, "}");
   }
   ralloc_strcat(j, "]}");
   return true;
}

/* ---- textures (meta::textures::texture_shape_from_name) ---- */

struct air_rf_shape {
   const char *dimension, *component, *format;
   bool arrayed, multisampled, writable, array_ref;
   unsigned array_length;        /* array<texture, N>: N (meta::texture_handle_array); 0 for none or an array_ref */
};

static struct air_rf_shape
air_rf_texture_shape(const char *name)
{
   struct air_rf_shape s = { "D2", "Float", NULL, false, false, false, false };
   /* texture_handle_array: array_ref<texture...> or array<texture...> */
   char compact[256];
   size_t c = 0;
   for (const char *p = name; *p && c + 1 < sizeof compact; p++) {
      if (*p != ' ' && *p != '\t' && *p != '\n')
         compact[c++] = *p;
   }
   compact[c] = 0;
   s.array_ref = !strncmp(compact, "array_ref<texture", 17) || !strncmp(compact, "array_ref<depth", 15) ||
                 !strncmp(compact, "array<texture", 13) || !strncmp(compact, "array<depth", 11);
   if (s.array_ref && compact[5] == '<') {
      /* the number after the last ',' before the last '>' */
      const char *gt = strrchr(compact, '>'), *comma = NULL;
      for (const char *p = compact; gt && p < gt; p++)
         if (*p == ',')
            comma = p;
      char *e = NULL;
      unsigned long n = comma ? strtoul(comma + 1, &e, 10) : 0;
      if (comma && e == gt && e > comma + 1 && n <= UINT32_MAX)
         s.array_length = (unsigned)n;
   }
   /* texture_access_from_name: the second field inside the first <...> is write or read_write */
   const char *lt = strchr(name, '<');
   if (lt) {
      const char *gt = strchr(lt + 1, '>');
      size_t inner = gt ? (size_t)(gt - lt - 1) : strlen(lt + 1);
      char buf[128];
      if (inner < sizeof buf) {
         memcpy(buf, lt + 1, inner);
         buf[inner] = 0;
         char *comma = strchr(buf, ',');
         if (comma) {
            char *f = comma + 1, *end = strchr(f, ',');
            if (end)
               *end = 0;
            while (*f == ' ' || *f == '\t')
               f++;
            size_t fl = strlen(f);
            while (fl && (f[fl - 1] == ' ' || f[fl - 1] == '\t'))
               f[--fl] = 0;
            s.writable = !strcmp(f, "write") || !strcmp(f, "read_write");
         }
      }
   }
   const char *shape = name;
   if (s.array_ref) {
      const char *a = strstr(name, "texture");
      if (!a)
         a = strstr(name, "depth");
      if (a)
         shape = a;
   }
   char head[128];
   const char *hl = strchr(shape, '<');
   size_t hn = hl ? (size_t)(hl - shape) : strlen(shape);
   if (hn >= sizeof head)
      hn = sizeof head - 1;
   memcpy(head, shape, hn);
   head[hn] = 0;
   if (strstr(head, "texture_buffer"))
      s.dimension = "Buffer";
   else if (strstr(head, "1d"))
      s.dimension = "D1";
   else if (strstr(head, "3d"))
      s.dimension = "D3";
   else if (strstr(head, "cube"))
      s.dimension = "Cube";
   s.arrayed = hn >= 6 && !strcmp(head + hn - 6, "_array");
   s.multisampled = strstr(head, "_ms") != NULL;
   /* texture_component_from_name: the first word after the first '<' (after a nested '<') */
   const char *r = strchr(shape, '<');
   if (r) {
      r++;
      while (*r == ',' || *r == '>' || *r == ' ' || *r == '\t' || *r == '\n')
         r++;
      char word[64];
      size_t w = 0;
      while (r[w] && r[w] != ',' && r[w] != '>' && r[w] != ' ' && r[w] != '\t' && r[w] != '\n' && w + 1 < sizeof word) {
         word[w] = r[w];
         w++;
      }
      word[w] = 0;
      const char *last = strrchr(word, '<');
      const char *scalar = last ? last + 1 : word;
      if (!strcmp(scalar, "uint") || !strcmp(scalar, "ushort") || !strcmp(scalar, "uchar"))
         s.component = "Uint";
      else if (!strcmp(scalar, "int") || !strcmp(scalar, "short") || !strcmp(scalar, "char"))
         s.component = "Sint";
   }
   if (s.writable) {
      if (!strcmp(s.component, "Float"))
         s.format = strstr(name, "<half") ? "Rgba16f" : strstr(name, "<float") ? "R32f" : "Rgba32f";
      else if (!strcmp(s.component, "Uint"))
         s.format = strstr(name, "<ushort") ? "Rgba16ui" : "Rgba8ui";
      else
         s.format = strstr(name, "<short") ? "Rgba16i" : "Rgba8i";
   }
   return s;
}

/* ---- static samplers (reflect::StaticSamplerState::from_air_words) ---- */

static float
air_rf_half(uint16_t h)
{
   return _mesa_half_to_float(h);
}

static bool
air_rf_sampler_state(char **j, const uint64_t words[2])
{
   const uint64_t w = words[0];
   static const char *const border[] = { "TransparentBlack", "OpaqueBlack", "OpaqueWhite" };
   static const char *const filter[] = { "Nearest", "Linear", "Bicubic" };
   static const char *const mip[] = { "None", "Nearest", "Linear" };
   static const char *const compare[] = { "None", "Less", "LessEqual", "Greater", "GreaterEqual", "Equal", "NotEqual",
                                          "Always", "Never" };
   static const char *const reduction[] = { "WeightedAverage", "Minimum", "Maximum" };
   const unsigned b = (unsigned)((w >> 56) & 3), mf = (unsigned)((w >> 13) & 3), cf = (unsigned)((w >> 16) & 0xf),
                  rd = (unsigned)((w >> 58) & 3), fmin = (unsigned)((w >> 11) & 3), fmag = (unsigned)((w >> 9) & 3);
   if (b > 2 || mf > 2 || cf > 8 || rd > 2 || fmin > 2 || fmag > 2)
      return false;
   const char *addr[3];
   for (unsigned k = 0; k < 3; k++) {
      unsigned a = (unsigned)((w >> (3 * k)) & 7);
      if (a > 3)
         return false;
      addr[k] = a == 0 ? (b ? "ClampToBorder" : "ClampToZero") : a == 1 ? "ClampToEdge" : a == 2 ? "Repeat" : "MirroredRepeat";
   }
   ralloc_asprintf_append(j, "{\"min_filter\":\"%s\",\"mag_filter\":\"%s\",\"mip_filter\":\"%s\",\"address_mode_s\":\"%s\","
                          "\"address_mode_t\":\"%s\",\"address_mode_r\":\"%s\",\"coordinates\":\"%s\",\"compare_function\":\"%s\","
                          "\"max_anisotropy\":%u,\"lod_min_clamp\":%.9g,\"lod_max_clamp\":%.9g,\"border_color\":\"%s\","
                          "\"reduction\":\"%s\",\"lod_bias\":%.9g,\"raw_words\":[%llu,%llu]}",
                          filter[fmin], filter[fmag], mip[mf], addr[0], addr[1], addr[2], ((w >> 15) & 1) ? "Pixel" : "Normalized",
                          compare[cf], (unsigned)((w >> 20) & 0xf) + 1, (double)air_rf_half((uint16_t)((w >> 24) & 0xffff)),
                          (double)air_rf_half((uint16_t)((w >> 40) & 0xffff)), border[b], reduction[rd],
                          (double)air_rf_half((uint16_t)(words[1] & 0xffff)), (unsigned long long)words[0],
                          (unsigned long long)words[1]);
   return true;
}

/* ---- the reflection ---- */

/* The entry's parameters as the module has them before the passes run (the translator reads the text it was given):
 * used, readnone, readonly, writeonly */
static LLVMValueRef
air_rf_entry(struct air_tr *t)
{
   static const char *const lists[] = { "air.kernel", "air.vertex", "air.fragment" };
   LLVMValueRef found = NULL;
   for (unsigned l = 0; l < ARRAY_SIZE(lists); l++) {
      unsigned n = L.LLVMGetNamedMetadataNumOperands(t->module, lists[l]);
      if (!n)
         continue;
      LLVMValueRef *nodes = rzalloc_array(t->mem, LLVMValueRef, n);
      L.LLVMGetNamedMetadataOperands(t->module, lists[l], nodes);
      for (unsigned i = 0; i < n; i++) {
         LLVMValueRef *ops;
         unsigned c = air_md_operands(nodes[i], &ops, t->mem);
         if (c < 3 || !ops[0] || !L.LLVMIsAFunction(ops[0]))
            continue;
         if (t->req->entry && strcmp(air_name(ops[0], NULL), t->req->entry))
            continue;
         if (found)
            return NULL;
         found = ops[0];
      }
   }
   return found;
}

static void
air_rf_before_passes(struct air_tr *t)
{
   LLVMValueRef f = air_rf_entry(t);
   t->rf_rewrite = false;
   if (air_has_fc_values(t)) {
      for (LLVMValueRef g = L.LLVMGetFirstGlobal(t->module); g && !t->rf_rewrite; g = L.LLVMGetNextGlobal(g)) {
         uint32_t index, size;
         t->rf_rewrite = air_fc_index(g, &index) && air_fc_given(t, index, &size);
      }
   }
   if (!f)
      return;
   const unsigned n = L.LLVMCountParams(f);
   const unsigned kn = L.LLVMGetEnumAttributeKindForName("readnone", 8),
                  ko = L.LLVMGetEnumAttributeKindForName("readonly", 8),
                  kw = L.LLVMGetEnumAttributeKindForName("writeonly", 9);
   t->rf_param = rzalloc_array(t->mem, uint8_t, n + 1);
   t->rf_params = n;
   for (unsigned p = 0; p < n; p++) {
      LLVMValueRef v = L.LLVMGetParam(f, p);
      uint8_t m = L.LLVMGetFirstUse(v) ? AIR_RF_USED : 0;
      if (kn && L.LLVMGetEnumAttributeAtIndex(f, p + 1, kn))
         m |= AIR_RF_READNONE;
      if (ko && L.LLVMGetEnumAttributeAtIndex(f, p + 1, ko))
         m |= AIR_RF_READONLY;
      if (kw && L.LLVMGetEnumAttributeAtIndex(f, p + 1, kw))
         m |= AIR_RF_WRITEONLY;
      t->rf_param[p] = m;
   }
}

/* reflect::ShaderReflection::refine_buffer_access_from_entry */
static const char *
air_rf_refined_access(struct air_tr *t, uint32_t param, const char *access)
{
   if (param >= t->rf_params)
      return access;
   const uint8_t m = t->rf_param[param];
   if (!(m & AIR_RF_USED) || (m & AIR_RF_READNONE))
      return "Unused";
   if (m & AIR_RF_READONLY)
      return "ReadOnly";
   if (m & AIR_RF_WRITEONLY)
      return "WriteOnly";
   return access;
}

struct air_rf_state {
   char *b;              /* the bindings, comma-separated */
   unsigned bindings;
   uint8_t occupied[704]; /* descriptor bindings taken (the band of the static samplers is 160 to 191) */
};

static void
air_rf_binding_head(struct air_rf_state *s, const char *kind, uint32_t metal, bool have_desc, uint32_t binding,
                    uint32_t count, bool have_param, uint32_t param)
{
   ralloc_asprintf_append(&s->b, "%s{\"kind\":\"%s\",\"metal_index\":%u,\"descriptor\":", s->bindings++ ? "," : "", kind, metal);
   if (have_desc) {
      ralloc_asprintf_append(&s->b, "{\"set\":0,\"binding\":%u,\"count\":%u}", binding, count);
      if (binding < ARRAY_SIZE(s->occupied))
         s->occupied[binding] = 1;
   } else
      ralloc_strcat(&s->b, "null");
   ralloc_strcat(&s->b, ",\"param_index\":");
   air_rf_jopt(&s->b, have_param, param);
   ralloc_strcat(&s->b, ",\"stage_input_location\":null");
}

/* a Buffer or ThreadgroupBuffer binding of the argument node x (parameter idx, slot n) */
static void
air_rf_buffer(struct air_tr *t, struct air_rf_state *s, const struct air_rf_node *x, uint32_t idx, uint32_t n,
              bool kernel, LLVMValueRef param)
{
   uint32_t space = 0, size = 0, object = 0;
   bool have_space = air_rf_i32_after(x, "air.address_space", &space);
   if (!have_space && param && L.LLVMGetTypeKind(L.LLVMTypeOf(param)) == TY_POINTER) {
      /* the parameter's pointer type: the translator reads addrspace(N) off the define line */
      space = L.LLVMGetPointerAddressSpace(L.LLVMTypeOf(param));
      have_space = space != 0;
   }
   if (!have_space && kernel) {
      space = 1;
      have_space = true;
   }
   const bool threadgroup = kernel && have_space && space == 3;
   const bool have_size = air_rf_i32_after(x, "air.arg_type_size", &size) || air_rf_i32_after(x, "air.buffer_size", &size);
   const bool have_object = air_rf_i32_after(x, "air.buffer_size", &object);
   const char *type_name = air_rf_string_after(x, "air.arg_type_name");
   air_rf_binding_head(s, threadgroup ? "ThreadgroupBuffer" : "Buffer", n, !threadgroup && n < 32, n, 1, true, idx);
   ralloc_strcat(&s->b, ",\"address_space\":");
   air_rf_jopt(&s->b, have_space, space);
   ralloc_strcat(&s->b, ",\"declared_size\":");
   air_rf_jopt(&s->b, have_size, size);
   if (have_object)
      ralloc_asprintf_append(&s->b, ",\"extent\":{\"Object\":{\"bytes\":%u}}", object);
   else
      ralloc_asprintf_append(&s->b, ",\"extent\":\"%s\"", have_size || type_name ? "Unbounded" : "Unknown");
   ralloc_strcat(&s->b, ",\"footprint\":null,\"type_layout\":");
   int k = air_rf_find(x, "air.struct_type_info");
   if (k < 0 || (unsigned)k + 1 >= x->n || !air_rf_struct(t, &s->b, x->ops[k + 1], 0))
      ralloc_strcat(&s->b, "null");
   ralloc_strcat(&s->b, ",\"type_name\":");
   air_rf_jstr(&s->b, type_name);
   ralloc_strcat(&s->b, ",\"texture_shape\":null,\"embedded_source\":null,\"access\":");
   const char *access = air_rf_declared_access(x);
   if (!access && have_space && space == 2)
      access = "ReadOnly";
   access = air_rf_refined_access(t, idx, access);
   air_rf_jstr(&s->b, access);
   ralloc_strcat(&s->b, ",\"static_sampler\":null}");
}

static void
air_rf_texture(struct air_tr *t, struct air_rf_state *s, const struct air_rf_node *x, uint32_t idx, uint32_t n)
{
   const char *type_name = air_rf_string_after(x, "air.arg_type_name");
   struct air_rf_shape sh = type_name ? air_rf_texture_shape(type_name) : (struct air_rf_shape){ 0 };
   const char *kind = !type_name ? "Texture" : sh.array_ref ? "TextureArray" : sh.writable ? "StorageImage" : "Texture";
   const char *access = type_name && sh.writable ? "Storage" : "Sampled";
   const bool storage = !strcmp(access, "Storage");
   const uint32_t binding = (storage ? 480 : 32) + n;
   /* TextureShape::descriptor_count: N, an array_ref's 128 (TEXTURE_HANDLE_ARRAY_DESCRIPTOR_COUNT), else 1 */
   air_rf_binding_head(s, kind, n, n < 128, binding, sh.array_ref ? (sh.array_length ? sh.array_length : 128) : 1, true, idx);
   ralloc_strcat(&s->b, ",\"address_space\":null,\"declared_size\":null,\"extent\":null,\"footprint\":null,"
                        "\"type_layout\":null,\"type_name\":");
   air_rf_jstr(&s->b, type_name);
   ralloc_strcat(&s->b, ",\"texture_shape\":");
   if (type_name) {
      ralloc_asprintf_append(&s->b, "{\"dimension\":\"%s\",\"arrayed\":%s,\"multisampled\":%s,\"component\":\"%s\","
                             "\"writable\":%s,\"array_ref\":%s,\"array_length\":",
                             sh.dimension, sh.arrayed ? "true" : "false", sh.multisampled ? "true" : "false", sh.component,
                             sh.writable ? "true" : "false", sh.array_ref ? "true" : "false");
      air_rf_jopt(&s->b, sh.array_length != 0, sh.array_length);
      ralloc_strcat(&s->b, ",\"storage_format\":");
      air_rf_jstr(&s->b, sh.format);
      ralloc_strcat(&s->b, "}");
   } else
      ralloc_strcat(&s->b, "null");
   ralloc_asprintf_append(&s->b, ",\"embedded_source\":null,\"access\":\"%s\",\"static_sampler\":null}", access);
}

static void
air_rf_sampler(struct air_tr *t, struct air_rf_state *s, const struct air_rf_node *x, uint32_t idx, uint32_t n)
{
   uint32_t slot, count = 0;
   if (air_rf_location(t, x, &slot, &count) <= 0 || count <= 1)
      count = 1;
   air_rf_binding_head(s, "Sampler", n, n < 32, 160 + n, count, true, idx);
   ralloc_strcat(&s->b, ",\"address_space\":null,\"declared_size\":null,\"extent\":null,\"footprint\":null,"
                        "\"type_layout\":null,\"type_name\":null,\"texture_shape\":null,\"embedded_source\":null,"
                        "\"access\":null,\"static_sampler\":null}");
}

static int
air_rf_loc_cmp(const void *a, const void *b)
{
   const uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
   return x < y ? -1 : x > y;
}

/* Writes t->rf_json, or leaves it NULL with t->rf_why. Called once the function is translated (t->statics holds the
 * module's constexpr samplers in the translator's order). */
static void
air_reflect(struct air_tr *t, LLVMValueRef args_node)
{
   const bool kernel = t->stage == NVMTL_AIR_NIR_STAGE_KERNEL, vertex = t->stage == NVMTL_AIR_NIR_STAGE_VERTEX;
   struct air_rf_state *s = rzalloc(t->mem, struct air_rf_state);
   s->b = ralloc_strdup(t->mem, "");
   char *attrs = ralloc_strdup(t->mem, ""), *vary = ralloc_strdup(t->mem, "");
   unsigned nattrs = 0, nvary = 0;
   /* a fragment's varyings are listed by Location (a BTreeSet in the translator), a vertex's in their order */
   struct air_rf_item { uint32_t loc; char *json; } fvary[64], vattr[64];
   unsigned nfvary = 0;
   uint32_t vin_loc = 0, frag_loc = 0;
   LLVMValueRef *nodes = NULL;
   const unsigned n = args_node && L.LLVMIsAMDNode(args_node) ? air_md_operands(args_node, &nodes, t->mem) : 0;
   if (!t->rf_param) {
      t->rf_why = "the entry's parameters were not read before the passes";
      return;
   }
   for (unsigned i = 0; i < n; i++) {
      struct air_rf_node x = air_rf_node(t, nodes[i]);
      uint32_t idx;
      bool have_idx = false;
      for (unsigned k = 0; k < x.n && !have_idx; k++)
         have_idx = air_rf_i32(x.ops[k], &idx);
      if (!have_idx)
         continue;
      const char *roles[64];
      const unsigned nr = air_rf_roles(t, &x, roles, ARRAY_SIZE(roles));
      if (!nr)
         continue;
      const char *first = roles[0], *primary = air_rf_primary(roles, nr);
      LLVMValueRef param = idx < L.LLVMCountParams(t->function) ? L.LLVMGetParam(t->function, idx) : NULL;
      bool global_slot = false;
      const uint32_t slot = air_rf_slot(t, &x, idx, &global_slot);
      /* the role each stage's parse takes: a kernel's and a vertex function's promote a gated resource (not a buffer),
       * a fragment function's take the primary role of every argument */
      const char *role;
      if (!kernel && !vertex)
         role = primary;
      else if (strcmp(first, "function_constant"))
         role = first;
      else if (primary && (!strcmp(primary, "texture") || !strcmp(primary, "sampler") || !strcmp(primary, "stage_in") ||
                           !strcmp(primary, "imageblock") || !strcmp(primary, "visible_function_table") ||
                           !strcmp(primary, "intersection_function_table") ||
                           (vertex && (!strcmp(primary, "vertex_input") || !strcmp(primary, "patch_input")))))
         role = primary;
      else
         role = first;   /* "function_constant": a gated buffer whose gate is not known is not bound */
      if (!role)
         continue;
      /* a texture whose slot a function constant sums and whose gate is known false is absent */
      if (!strcmp(role, "texture") && global_slot && !strcmp(first, "function_constant") && x.n &&
          air_rf_find(&x, "air.function_constant") >= 0) {
         int g = air_rf_find(&x, "air.function_constant");
         if ((unsigned)g + 1 < x.n && air_rf_gate_value(t, x.ops[g + 1]) == 0)
            continue;
      }
      if (!strcmp(role, "buffer")) {
         air_rf_buffer(t, s, &x, idx, slot, kernel, param);
      } else if (!strcmp(role, "texture")) {
         air_rf_texture(t, s, &x, idx, slot);
      } else if (!strcmp(role, "sampler")) {
         air_rf_sampler(t, s, &x, idx, slot);
      } else if (vertex && !strcmp(role, "vertex_input")) {
         uint32_t loc = vin_loc++;
         bool g;
         loc = air_rf_slot(t, &x, loc, &g);
         /* by Location (a BTreeSet), a later argument at the same Location replacing what an earlier one said */
         unsigned at = 0;
         while (at < nattrs && vattr[at].loc != loc)
            at++;
         if (at == nattrs) {
            if (nattrs >= ARRAY_SIZE(vattr)) {
               t->rf_why = "more than 64 vertex attributes";
               return;
            }
            nattrs++;
         }
         char *v = ralloc_asprintf(t->mem, "{\"location\":%u,\"type_name\":", loc);
         air_rf_jstr(&v, air_rf_string_after(&x, "air.arg_type_name"));
         ralloc_strcat(&v, ",\"name\":");
         air_rf_jstr(&v, air_rf_string_after(&x, "air.arg_name"));
         ralloc_strcat(&v, "}");
         vattr[at].loc = loc;
         vattr[at].json = v;
      } else if (!kernel && !vertex && !strcmp(role, "fragment_input")) {
         if (nfvary >= ARRAY_SIZE(fvary)) {
            t->rf_why = "more than 64 fragment inputs";
            return;
         }
         char *v = ralloc_asprintf(t->mem, "{\"location\":%u,\"type_name\":", frag_loc);
         air_rf_jstr(&v, air_rf_string_after(&x, "air.arg_type_name"));
         ralloc_strcat(&v, ",\"name\":");
         air_rf_jstr(&v, air_rf_string_after(&x, "air.arg_name"));
         ralloc_strcat(&v, ",\"user_semantic\":");
         air_rf_jstr(&v, air_rf_string_after(&x, "air.fragment_input"));
         ralloc_strcat(&v, "}");
         fvary[nfvary].loc = frag_loc++;
         fvary[nfvary++].json = v;
      } else if (!kernel && !vertex && !strcmp(role, "render_target") && (t->req->flags & NVMTL_AIR_NIR_COLOR_INPUTS)) {
         /* a [[color(n)]] input (reflect FragRole::ColorInput): n is meta::declared_slot of air.render_target (a literal,
          * or a global's known value, else the parameter's index), its descriptor the colour-input band's 192 + n */
         uint32_t n = idx;
         const int k = air_rf_find(&x, "air.render_target");
         if (k >= 0 && (unsigned)k + 1 < x.n && !air_rf_i32(x.ops[k + 1], &n)) {
            LLVMValueRef g = x.ops[k + 1];
            while (g && L.LLVMIsAConstantExpr(g))
               g = L.LLVMGetOperand(g, 0);
            LLVMValueRef init = g && L.LLVMIsAGlobalVariable(g) && L.LLVMIsGlobalConstant(g) && !L.LLVMIsExternallyInitialized(g)
                                ? L.LLVMGetInitializer(g) : NULL;
            n = init && L.LLVMIsAConstantInt(init) ? (uint32_t)L.LLVMConstIntGetZExtValue(init) : idx;
         }
         air_rf_binding_head(s, "ColorInput", n, n < 8, NVMTL_AIR_NIR_FETCH_BINDING + n, 1, true, idx);
         ralloc_strcat(&s->b, ",\"address_space\":null,\"declared_size\":null,\"extent\":null,\"footprint\":null,"
                              "\"type_layout\":null,\"type_name\":");
         air_rf_jstr(&s->b, air_rf_string_after(&x, "air.arg_type_name"));
         ralloc_strcat(&s->b, ",\"texture_shape\":null,\"embedded_source\":null,\"access\":null,\"static_sampler\":null}");
      } else if (!strcmp(role, "indirect_buffer") || !strcmp(role, "render_target") || !strcmp(role, "stage_in") ||
                 !strcmp(role, "imageblock") || !strcmp(role, "visible_function_table") ||
                 !strcmp(role, "intersection_function_table") || !strcmp(role, "instance_acceleration_structure") ||
                 !strcmp(role, "primitive_acceleration_structure") || !strcmp(role, "patch_input")) {
         /* refused by the translation; a reflection of it is not this frontend's to give */
         t->rf_why = ralloc_asprintf(t->mem, "an argument of the role %s", role);
         return;
      }
   }
   /* a vertex function's varyings: its outputs, in order (meta: the vertex output roles) */
   if (vertex) {
      LLVMValueRef *outs = NULL;
      const unsigned no = t->outputs_node && L.LLVMIsAMDNode(t->outputs_node) ? air_md_operands(t->outputs_node, &outs, t->mem) : 0;
      uint32_t out_loc = 0;
      for (unsigned i = 0; i < no; i++) {
         struct air_rf_node x = air_rf_node(t, outs[i]);
         const char *roles[64];
         const unsigned nr = air_rf_roles(t, &x, roles, ARRAY_SIZE(roles));
         if (!nr)
            continue;
         const char *first = roles[0];
         if (!strcmp(first, "function_constant")) {
            const char *p = air_rf_primary(roles, nr);
            if (p && (!strcmp(p, "vertex_output") || !strcmp(p, "position") || !strcmp(p, "point_size") ||
                      !strcmp(p, "clip_distance") || !strcmp(p, "viewport_array_index") ||
                      !strcmp(p, "render_target_array_index")))
               first = p;
         }
         if (strcmp(first, "vertex_output"))
            continue;
         bool g;
         const uint32_t loc = air_rf_slot(t, &x, out_loc, &g);
         out_loc++;
         ralloc_asprintf_append(&vary, "%s{\"location\":%u,\"type_name\":", nvary++ ? "," : "", loc);
         air_rf_jstr(&vary, air_rf_string_after(&x, "air.arg_type_name"));
         ralloc_strcat(&vary, ",\"name\":");
         air_rf_jstr(&vary, air_rf_string_after(&x, "air.arg_name"));
         ralloc_strcat(&vary, ",\"user_semantic\":");
         air_rf_jstr(&vary, air_rf_string_after(&x, "air.vertex_output"));
         ralloc_strcat(&vary, "}");
      }
   } else if (!kernel) {
      /* by Location: the inputs are numbered in order, so their order is the Locations' */
      for (unsigned i = 0; i < nfvary; i++)
         ralloc_asprintf_append(&vary, "%s%s", nvary++ ? "," : "", fvary[i].json);
   }
   qsort(vattr, nattrs, sizeof vattr[0], air_rf_loc_cmp);
   for (unsigned i = 0; i < nattrs; i++)
      ralloc_asprintf_append(&attrs, "%s%s", i ? "," : "", vattr[i].json);
   /* the constexpr samplers: each the first free binding of the sampler band, in the translator's order */
   util_dynarray_foreach(&t->statics, struct air_sampler *, ps) {
      unsigned binding = 160;
      while (binding < 192 && s->occupied[binding])
         binding++;
      if (binding >= 192) {
         t->rf_why = "more constexpr samplers than the sampler band holds";
         return;
      }
      air_rf_binding_head(s, "StaticSampler", binding - 160, true, binding, 1, false, 0);
      ralloc_strcat(&s->b, ",\"address_space\":null,\"declared_size\":null,\"extent\":null,\"footprint\":null,"
                           "\"type_layout\":null,\"type_name\":null,\"texture_shape\":null,\"embedded_source\":null,"
                           "\"access\":null,\"static_sampler\":");
      if (!air_rf_sampler_state(&s->b, (*ps)->words)) {
         t->rf_why = "a constexpr sampler's state the translator does not decode";
         return;
      }
      ralloc_strcat(&s->b, "}");
   }
   if (t->rf_computed_gate) {
      t->rf_why = "a function-constant gate the static initializer computes (the translator's evaluation may not know it)";
      return;
   }
   char *j = ralloc_asprintf(t->mem, "{\"reflection_source\":\"air-to-nir\",\"stage\":\"%s\",\"entry_point\":",
                             kernel ? "Kernel" : vertex ? "Vertex" : "Fragment");
   air_rf_jstr(&j, air_name(t->function, NULL));
   ralloc_asprintf_append(&j, ",\"bindings\":[%s],\"argument_buffer_fields\":[],\"vertex_attributes\":[%s],\"varyings\":[%s],"
                          "\"tessellation\":null,\"fragment_sample_positions\":null,\"local_size\":",
                          s->b, attrs, vary);
   /* a kernel's default size: the translator's (TransformOptions::default().kernel_local_size), its SpecIds' defaults */
   ralloc_strcat(&j, kernel ? "[64,1,1]}" : "null}");
   t->rf_json = j;
}

static nir_shader *
air_translate(struct air_tr *t, const struct nvk_physical_device *pdev, const struct vk_pipeline_robustness_state *rs)
{
   LLVMValueRef args_node = NULL;
   t->function = air_find_kernel(t, &args_node);
   t->layout = L.LLVMGetModuleDataLayout(t->module);

   const nir_shader_compiler_options *options = nak_nir_options(pdev->nak);
   const bool kernel = t->stage == NVMTL_AIR_NIR_STAGE_KERNEL;
   t->set = t->stage == NVMTL_AIR_NIR_STAGE_FRAGMENT ? NVMTL_AIR_NIR_FRAGMENT_SET : 0;
   t->nir = nir_shader_create(NULL, kernel ? MESA_SHADER_COMPUTE : t->stage == NVMTL_AIR_NIR_STAGE_VERTEX ?
                                    MESA_SHADER_VERTEX : MESA_SHADER_FRAGMENT, options);
   t->nir->info.name = ralloc_strdup(t->nir, air_name(t->function, NULL));
   for (unsigned i = 0; kernel && i < 3; i++)
      t->nir->info.workgroup_size[i] = t->req->local_size[i];
   if (t->stage == NVMTL_AIR_NIR_STAGE_FRAGMENT)
      t->nir->info.fs.early_fragment_tests = (t->fragment_flags & NVMTL_AIR_NIR_FS_EARLY_TESTS) != 0;
   t->nir->info.api_subgroup_size = 32;
   t->nir->info.max_subgroup_size = 32;
   t->nir->info.min_subgroup_size = 32;

   nir_function *fn = nir_function_create(t->nir, "main");
   fn->is_entrypoint = true;
   t->impl = nir_function_impl_create(fn);
   t->impl->structured = false;
   t->b = nir_builder_at(nir_after_impl(t->impl));
   t->top = nir_builder_at(nir_before_impl(t->impl));

   if (kernel) {   /* a vertex or fragment function reads no push constants */
      struct glsl_struct_field fields[12];
      for (unsigned i = 0; i < 12; i++)
         fields[i] = (struct glsl_struct_field) { .type = glsl_uint_type(), .name = "w", .offset = (int)(4 * i) };
      t->push = nir_variable_create(t->nir, nir_var_mem_push_const,
                                    glsl_interface_type(fields, 12, GLSL_INTERFACE_PACKING_STD430, false, "grid"), "grid");
   }

   if (t->req->flags & NVMTL_AIR_NIR_TABLE_SSBO) {
      t->table_mode = nir_var_mem_ssbo;
      t->table_format = nvk_ssbo_addr_format(pdev, rs);
   } else {
      t->table_mode = nir_var_mem_ubo;
      t->table_format = nvk_ubo_addr_format(pdev, rs);
   }
   t->table_type = glsl_array_type(glsl_uint64_t_type(), NVMTL_AIR_NIR_MAX_BUFFERS, 8);

   air_bind_arguments(t, args_node);
   if (t->req->flags & NVMTL_AIR_NIR_REFLECT)
      air_reflect(t, args_node);
   air_emit_function(t);

   t->nir->info.shared_size = t->shared_size;
   t->nir->scratch_size = ALIGN_POT(t->scratch_size, 16);
   unsigned cd = util_dynarray_num_elements(&t->constant_data, uint8_t);
   if (cd) {
      t->nir->constant_data = ralloc_size(t->nir, cd);
      memcpy(t->nir->constant_data, t->constant_data.data, cd);
      t->nir->constant_data_size = cd;
   }
   if (t->shared_size > 48 * 1024)
      unsupported(t, "%u bytes of threadgroup memory", t->shared_size);

   nir_lower_goto_ifs(t->nir);
   NIR_PASS(_, t->nir, nir_lower_vars_to_ssa);
   NIR_PASS(_, t->nir, nir_opt_dce);
   if (!kernel)
      nir_shader_gather_info(t->nir, nir_shader_get_entrypoint(t->nir));
   nir_validate_shader(t->nir, "after AIR to NIR");
   return t->nir;
}

/* A text module that defines one global twice (two linked modules' @__air_sampler_state of one constexpr sampler):
 * LLVM's parser refuses it. A later definition with the same type and initializer as the first (the lines equal
 * once a trailing ", align N" is set aside) is dropped; one that differs is not this frontend's to choose between.
 * The same for functions: the translator's linker appends a linked module after the entry, so the text declares a
 * linked function the entry calls and then defines it, and declares an intrinsic both modules call twice; LLVM takes
 * a name once, and the translator's reader takes the definition. A declaration of a function the text defines is
 * dropped, and a later declaration of a function declared before with the same signature (the line up to the end of
 * its parameters; the attribute groups are the entry module's after the link) is dropped; one with another signature
 * is refused. 0: nothing repeated; 1: *out (malloc'd, *out_n bytes) is the text without the repeats; -1: a global or
 * a function given twice differently, named in why. Globals are the lines that begin with '@' (a function body's
 * lines do not), functions the lines that begin with "define " or "declare ". */
static size_t
air_global_def_len(const char *line, size_t len)
{
   /* the definition without a trailing ", align N" */
   size_t n = len;
   while (n && (line[n - 1] == ' ' || line[n - 1] == '\r'))
      n--;
   size_t k = n;
   while (k && line[k - 1] >= '0' && line[k - 1] <= '9')
      k--;
   if (k < n && k >= 8 && !strncmp(line + k - 8, ", align ", 8))
      return k - 8;
   return n;
}

/* the name of the function a "define " or "declare " line gives (from its '@' to its '('), and in *sig_len the length of
 * the line up to the ')' that closes its parameters; NULL when the line is not one */
static char *
air_function_line_name(void *mem, const char *line, size_t len, size_t *sig_len)
{
   if (!(len > 7 && !strncmp(line, "define ", 7)) && !(len > 8 && !strncmp(line, "declare ", 8)))
      return NULL;
   const char *at = memchr(line, '@', len);
   if (!at)
      return NULL;
   const char *p = at + 1, *end = line + len;
   if (p < end && *p == '"') {
      p++;
      while (p < end && *p != '"')
         p++;
      p++;
   } else {
      while (p < end && *p != '(')
         p++;
   }
   if (p >= end || *p != '(')
      return NULL;
   char *name = ralloc_strndup(mem, at, p - at);
   unsigned depth = 0;
   for (const char *q = p; q < end; q++) {
      if (*q == '(')
         depth++;
      else if (*q == ')' && !--depth) {
         *sig_len = (size_t)(q + 1 - line);
         return name;
      }
   }
   return NULL;
}

static int
air_dedupe_globals(const char *text, size_t size, char **out, size_t *out_n, char *why, size_t why_n)
{
   void *mem = ralloc_context(NULL);
   struct hash_table *seen = _mesa_hash_table_create(mem, _mesa_hash_string, _mesa_key_string_equal);
   struct hash_table *defined = _mesa_hash_table_create(mem, _mesa_hash_string, _mesa_key_string_equal);
   struct hash_table *declared = _mesa_hash_table_create(mem, _mesa_hash_string, _mesa_key_string_equal);
   struct util_dynarray drop;   /* [start, end) of each line to leave out */
   util_dynarray_init(&drop, mem);
   int r = 0;
   /* the functions the text defines */
   for (size_t at = 0; at < size;) {
      const char *line = text + at;
      const char *nl = memchr(line, '\n', size - at);
      const size_t len = nl ? (size_t)(nl - line) : size - at;
      size_t sig = 0;
      char *name = len > 7 && !strncmp(line, "define ", 7) ? air_function_line_name(mem, line, len, &sig) : NULL;
      if (name)
         _mesa_hash_table_insert(defined, name, name);
      at += len + (nl ? 1 : 0);
   }
   for (size_t at = 0; at < size;) {
      const char *line = text + at;
      const char *nl = memchr(line, '\n', size - at);
      const size_t len = nl ? (size_t)(nl - line) : size - at;
      const size_t next = at + len + (nl ? 1 : 0);
      size_t sig = 0;
      char *fname = len > 8 && !strncmp(line, "declare ", 8) ? air_function_line_name(mem, line, len, &sig) : NULL;
      if (fname) {
         struct hash_entry *e = _mesa_hash_table_search(declared, fname);
         if (_mesa_hash_table_search(defined, fname) ||
             (e && strlen((const char *)e->data) == sig && !memcmp(e->data, line, sig))) {
            util_dynarray_append(&drop, at);
            util_dynarray_append(&drop, next);
            r = 1;
         } else if (e) {
            snprintf(why, why_n, "the text declares the function %s twice, differently", fname);
            r = -1;
            break;
         } else
            _mesa_hash_table_insert(declared, fname, ralloc_strndup(mem, line, sig));
      }
      if (len > 1 && line[0] == '@') {
         const char *eq = NULL;
         for (size_t i = 1; i + 2 < len; i++) {
            if (line[i] == '"') {
               while (++i < len && line[i] != '"')
                  ;
               continue;
            }
            if (line[i] == ' ' && line[i + 1] == '=' && line[i + 2] == ' ') {
               eq = line + i;
               break;
            }
         }
         if (eq) {
            char *name = ralloc_strndup(mem, line, eq - line);
            const size_t def = air_global_def_len(line, len);
            struct hash_entry *e = _mesa_hash_table_search(seen, name);
            if (!e) {
               char *body = ralloc_strndup(mem, line, def);
               _mesa_hash_table_insert(seen, name, body);
            } else if (strlen((const char *)e->data) == def && !memcmp(e->data, line, def)) {
               util_dynarray_append(&drop, at);
               util_dynarray_append(&drop, next);
               r = 1;
            } else {
               snprintf(why, why_n, "the text defines the global %s twice, differently", name);
               r = -1;
               break;
            }
         }
      }
      at = next;
   }
   if (r == 1) {
      const unsigned n = util_dynarray_num_elements(&drop, size_t) / 2;
      const size_t *d = util_dynarray_begin(&drop);
      char *o = malloc(size + 1);
      size_t w = 0, from = 0;
      if (!o) {
         snprintf(why, why_n, "no memory for the text without its repeated globals");
         r = -1;
      } else {
         for (unsigned i = 0; i < n; i++) {
            memcpy(o + w, text + from, d[2 * i] - from);
            w += d[2 * i] - from;
            from = d[2 * i + 1];
         }
         memcpy(o + w, text + from, size - from);
         w += size - from;
         o[w] = 0;
         *out = o;
         *out_n = w;
      }
   }
   ralloc_free(mem);
   return r;
}

void nvmtl_air_nir_free_v1(void *nir);
int nvmtl_air_to_nir_v1(void *device, const struct nvmtl_air_nir_request *req, struct nvmtl_air_nir_result *res);
void *nvmtl_air_nir_clone_v1(void *nir, const uint32_t local_size[3]);
int nvmtl_air_nir_serialize_v1(void *nir, void **data, size_t *size);
void *nvmtl_air_nir_deserialize_v1(void *device, const void *data, size_t size);
int nvmtl_air_nir_version_v1(void);

__attribute__((visibility("default"))) int
nvmtl_air_nir_version_v1(void)
{
   return NVMTL_AIR_NIR_VERSION;
}

/* The kernel for another threadgroup size: the translation reads the size only through nir_load_workgroup_size, which
 * NVK lowers from the shader's workgroup size, so a copy with that size changed is the kernel translated for it. */
__attribute__((visibility("default"))) void *
nvmtl_air_nir_clone_v1(void *nir, const uint32_t local_size[3])
{
   if (!nir || !local_size || !local_size[0] || !local_size[1] || !local_size[2] ||
       (uint64_t)local_size[0] * local_size[1] * local_size[2] > 1024)
      return NULL;
   nir_shader *copy = nir_shader_clone(NULL, (nir_shader *)nir);
   if (!copy)
      return NULL;
   for (unsigned i = 0; i < 3; i++)
      copy->info.workgroup_size[i] = local_size[i];
   return copy;
}

/* A result's shader as bytes, for the plugin's shader cache; only this build of NVK reads them back. */
__attribute__((visibility("default"))) int
nvmtl_air_nir_serialize_v1(void *nir, void **data, size_t *size)
{
   if (!nir || !data || !size)
      return -1;
   struct blob b;
   blob_init(&b);
   nir_serialize(&b, (const nir_shader *)nir, false);
   if (b.out_of_memory) {
      blob_finish(&b);
      return -1;
   }
   blob_finish_get_buffer(&b, data, size);
   return 0;
}

__attribute__((visibility("default"))) void *
nvmtl_air_nir_deserialize_v1(void *device, const void *data, size_t size)
{
   if (!device || !data || !size)
      return NULL;
   VK_FROM_HANDLE(nvk_device, dev, (VkDevice)device);
   const struct nvk_physical_device *pdev = nvk_device_physical(dev);
   struct blob_reader r;
   blob_reader_init(&r, data, size);
   nir_shader *nir = nir_deserialize(NULL, nak_nir_options(pdev->nak), &r);
   if (!nir || r.overrun || r.current != r.end) {
      ralloc_free(nir);
      return NULL;
   }
   return nir;
}

__attribute__((visibility("default"))) void
nvmtl_air_nir_free_v1(void *nir)
{
   ralloc_free(nir);
}

__attribute__((visibility("default"))) int
nvmtl_air_to_nir_v1(void *device, const struct nvmtl_air_nir_request *req, struct nvmtl_air_nir_result *res)
{
   if (!res || res->size < offsetof(struct nvmtl_air_nir_result, message) + 16)
      return NVMTL_AIR_NIR_BAD_REQUEST;
   /* both structs start with their size: an older caller's shorter result gets what it has room for */
   char *message = res->message;
   size_t message_len = res->size - offsetof(struct nvmtl_air_nir_result, message);
   if (message_len > sizeof res->message)
      message_len = sizeof res->message;
   const uint32_t res_size = MIN2(res->size, (uint32_t)sizeof *res);
   memset(res, 0, res_size);
   res->size = res_size;
   message[0] = 0;

   if (!device || !req || req->size < NVMTL_AIR_NIR_REQUEST_V1_SIZE || !req->bitcode || !req->bitcode_size || !req->llvm_library ||
       !req->local_size[0] || !req->local_size[1] || !req->local_size[2] ||
       (uint64_t)req->local_size[0] * req->local_size[1] * req->local_size[2] > 1024) {
      snprintf(message, message_len, "bad request");
      return res->status = NVMTL_AIR_NIR_BAD_REQUEST;
   }
   VK_FROM_HANDLE(nvk_device, dev, (VkDevice)device);
   const struct nvk_physical_device *pdev = nvk_device_physical(dev);
   if (!air_llvm_open(req->llvm_library, message, message_len))
      return res->status = NVMTL_AIR_NIR_NO_LLVM;

   /* the robustness the pipeline is created with: the table's address format depends on it */
   struct vk_pipeline_robustness_state rs;
   const VkPipelineRobustnessCreateInfoEXT unchecked = {
      VK_STRUCTURE_TYPE_PIPELINE_ROBUSTNESS_CREATE_INFO_EXT, NULL, VK_PIPELINE_ROBUSTNESS_BUFFER_BEHAVIOR_DISABLED_EXT,
      VK_PIPELINE_ROBUSTNESS_BUFFER_BEHAVIOR_DISABLED_EXT, VK_PIPELINE_ROBUSTNESS_BUFFER_BEHAVIOR_DEVICE_DEFAULT_EXT,
      VK_PIPELINE_ROBUSTNESS_IMAGE_BEHAVIOR_DEVICE_DEFAULT_EXT };
   vk_pipeline_robustness_state_fill(&dev->vk.robustness_state, &rs,
                                     (req->flags & NVMTL_AIR_NIR_UNCHECKED) ? &unchecked : NULL, NULL);

   /* on the heap: what changes between setjmp and longjmp must not live in this frame */
   struct air_tr *t = rzalloc(NULL, struct air_tr);
   if (!t)
      return res->status = NVMTL_AIR_NIR_NO_MEMORY;
   t->req = req;
   t->message = message;
   t->message_len = message_len;
   t->mem = t;
   t->values = _mesa_pointer_hash_table_create(t);
   t->blocks = _mesa_pointer_hash_table_create(t);
   t->globals = _mesa_pointer_hash_table_create(t);
   util_dynarray_init(&t->phis, t);
   util_dynarray_init(&t->statics, t);
   util_dynarray_init(&t->constant_data, t);

   LLVMContextRef ctx = L.LLVMContextCreate();
   /* without a handler of its own, LLVM ends the process on a bitcode error */
   L.LLVMContextSetDiagnosticHandler(ctx, air_llvm_diagnostic, t);
   /* LLVM's copy ends in the 0 its text parser wants. The text parser takes the buffer (on a failure too), so it is
    * disposed of here only after the bitcode reader; neither variable changes between setjmp and longjmp. */
   const bool text = (req->flags & NVMTL_AIR_NIR_TEXT_IR) != 0;
   char *deduped = NULL, dedupe_why[160] = "";
   size_t deduped_n = 0;
   const int dedupe = text ? air_dedupe_globals(req->bitcode, req->bitcode_size, &deduped, &deduped_n, dedupe_why,
                                                sizeof dedupe_why) : 0;
   LLVMMemoryBufferRef buf = dedupe < 0 ? NULL :
                             L.LLVMCreateMemoryBufferWithMemoryRangeCopy(deduped ? deduped : req->bitcode,
                                                                         deduped ? deduped_n : req->bitcode_size, "air");
   free(deduped);
   if (setjmp(t->fail) == 0) {
      if (dedupe < 0)
         air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "the text IR does not parse: %s", dedupe_why);
      if (buf && text) {
         char *msg = NULL;
         if (L.LLVMParseIRInContext(ctx, buf, &t->module, &msg)) {
            snprintf(t->llvm_error, sizeof t->llvm_error, "%s", msg ? msg : "?");
            if (msg)
               L.LLVMDisposeMessage(msg);
            air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "the text IR does not parse: %s", t->llvm_error);
         }
         if (msg)
            L.LLVMDisposeMessage(msg);
      } else if (!buf || L.LLVMParseBitcodeInContext2(ctx, buf, &t->module))
         air_refuse(t, NVMTL_AIR_NIR_BAD_AIR, "the bitcode does not parse%s%s", t->llvm_error[0] ? ": " : "",
                    t->llvm_error);
      bool fold = air_fc_specialize(t, ctx);
      if (req->flags & NVMTL_AIR_NIR_REFLECT)
         air_rf_before_passes(t);
      const char *dump_fc = getenv("NVMTL_AIR_NIR_DUMP_FC");   /* the module once the function constants are in */
      if (dump_fc && *dump_fc) {
         FILE *f = fopen(dump_fc, "w");
         char *text = f ? L.LLVMPrintModuleToString(t->module) : NULL;
         if (text) {
            fputs(text, f);
            L.LLVMDisposeMessage(text);
         }
         if (f)
            fclose(f);
      }
      if (!(req->flags & NVMTL_AIR_NIR_NO_PASSES))
         air_run_passes(t, ctx, fold);
      t->out = air_translate(t, pdev, &rs);
      t->status = NVMTL_AIR_NIR_OK;
   } else if (t->nir) {
      ralloc_free(t->nir);
      t->nir = NULL;
      t->out = NULL;
   }
   if (t->module)
      L.LLVMDisposeModule(t->module);
   if (buf && !text)
      L.LLVMDisposeMemoryBuffer(buf);
   L.LLVMContextDispose(ctx);

   res->status = t->status;
   res->nir = t->out;
   res->buffer_mask = t->buffer_mask;
   res->shared_bytes = t->shared_size;
   res->scratch_bytes = t->scratch_size;
   res->instructions = t->instructions;
   if (res->size >= offsetof(struct nvmtl_air_nir_result, static_samplers) + sizeof res->static_samplers && t->out) {
      memcpy(res->texture_mask, t->texture_mask, sizeof t->texture_mask);
      memcpy(res->storage_mask, t->storage_mask, sizeof t->storage_mask);
      memcpy(res->texbuf_mask, t->texbuf_mask, sizeof t->texbuf_mask);
      memcpy(res->stexbuf_mask, t->stexbuf_mask, sizeof t->stexbuf_mask);
      res->sampler_mask = t->sampler_mask;
      unsigned n = 0;
      util_dynarray_foreach(&t->statics, struct air_sampler *, ps) {
         if (n < ARRAY_SIZE(res->static_samplers)) {
            res->static_samplers[n].slot = (*ps)->slot;
            res->static_samplers[n].words[0] = (*ps)->words[0];
            res->static_samplers[n].words[1] = (*ps)->words[1];
         }
         n++;
      }
      res->static_sampler_count = n;
   }
   if (res->size >= offsetof(struct nvmtl_air_nir_result, global_align) + sizeof res->global_align && t->out)
      res->global_align = t->global_align;
   if (res->size >= offsetof(struct nvmtl_air_nir_result, reserved4) + sizeof res->reserved4 && t->out) {
      res->stage = t->stage;
      res->vertex_inputs = t->vertex_inputs;
      memcpy(res->vertex_input_kind, t->vertex_input_kind, sizeof res->vertex_input_kind);
      res->varyings = t->varyings;
      res->color_outputs = t->color_outputs;
      res->fragment_flags = t->fragment_flags;
   }
   if (res->size >= offsetof(struct nvmtl_air_nir_result, resource_flags) + sizeof res->resource_flags && t->out) {
      res->color_inputs = t->color_inputs;
      res->resource_flags = t->heap_textures ? NVMTL_AIR_NIR_RES_HEAP : 0;
   }
   if (res->size >= offsetof(struct nvmtl_air_nir_result, reflection_why) + sizeof res->reflection_why &&
       (req->flags & NVMTL_AIR_NIR_REFLECT) && t->out) {
      if (t->rf_json) {
         const size_t n = strlen(t->rf_json);
         res->reflection = malloc(n + 1);
         if (res->reflection) {
            memcpy(res->reflection, t->rf_json, n + 1);
            res->reflection_size = n;
         } else
            snprintf(res->reflection_why, sizeof res->reflection_why, "out of memory");
      } else
         snprintf(res->reflection_why, sizeof res->reflection_why, "%s", t->rf_why ? t->rf_why : "not reflected");
   }
   if (t->out)
      snprintf(message, message_len, "%s: %u instructions, buffers 0x%x, %u B threadgroup, %u B thread",
               t->out->info.name, t->instructions, t->buffer_mask, t->shared_size, t->scratch_size);
   ralloc_free(t);
   return res->status;
}
