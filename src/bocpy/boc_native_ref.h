/// @file boc_native_ref.h
/// @brief Owned references to interpreter-independent native allocations

#ifndef BOCPY_NATIVE_REF_H
#define BOCPY_NATIVE_REF_H

typedef struct boc_native_ref_ops {
  void (*retain)(void *);
  void (*release)(void *);
} BOCNativeRefOps;

typedef struct boc_native_ref {
  void *pointer;
  const BOCNativeRefOps *ops;
} BOCNativeRef;

extern const BOCNativeRefOps BOC_COWN_REF_OPS;

static inline void native_ref_retain(BOCNativeRef ref) {
  ref.ops->retain(ref.pointer);
}

static inline void native_ref_release(BOCNativeRef ref) {
  ref.ops->release(ref.pointer);
}

#endif // BOCPY_NATIVE_REF_H
