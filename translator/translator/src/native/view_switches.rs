//! The one way back from a change to code generation that is on by default: the whole-object views
//! of pull request 524 of the original project (its translator part T3, for League of Legends, with
//! the byte-view merge of its part T4, for the iOS Simulator).
//!
//! NVMTL_WHOLE_VIEWS=0: a local variable is retyped through any pointer derived from it, and a
//! helper that views a pointer-free object both as its aggregate and as bytes keeps the pointee it
//! had, as in 3.0.4 (native/ir/alloca.rs, native/ir/pointer_pointees.rs).
//!
//! It is read once per process. The plugin's settings file puts the name in the environment before
//! the first module is translated, and the plugin adds a mark to its shader cache tag when it is
//! off (plugin/nvmtl_spvcache_name.h), so a cached module translated in one mode is never served in
//! the other. Only a value whose first byte is 0 turns the change off. The raw bytes are read, as
//! the plugin reads them, so a value that is not UTF-8 means the same to both.
use std::ffi::OsStr;
use std::sync::OnceLock;

fn turned_off(value: Option<&OsStr>) -> bool {
    value.is_some_and(|v| v.as_encoded_bytes().first() == Some(&b'0'))
}

/// true unless NVMTL_WHOLE_VIEWS=0: only a proven whole-object view retypes a local variable, and a
/// mixed aggregate and byte view of a pointer-free object gets one byte array of the object's size.
pub(crate) fn whole_views_on() -> bool {
    #[cfg(test)]
    if let Some(on) = test_override::get() {
        return on;
    }
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| !turned_off(std::env::var_os("NVMTL_WHOLE_VIEWS").as_deref()))
}

/// The unit tests run both paths in one process, each test on a thread of its own: the switch can
/// be set for the calling thread.
#[cfg(test)]
pub(crate) mod test_override {
    use std::cell::Cell;

    thread_local! {
        static WHOLE_VIEWS: Cell<Option<bool>> = const { Cell::new(None) };
    }

    pub(super) fn get() -> Option<bool> {
        WHOLE_VIEWS.with(|cell| cell.get())
    }

    /// Runs `f` with the switch set to `on` on this thread, and puts the previous state back.
    pub(crate) fn with_whole_views<R>(on: bool, f: impl FnOnce() -> R) -> R {
        struct Restore(Option<bool>);
        impl Drop for Restore {
            fn drop(&mut self) {
                WHOLE_VIEWS.with(|cell| cell.set(self.0));
            }
        }
        let _restore = Restore(WHOLE_VIEWS.with(|cell| cell.replace(Some(on))));
        f()
    }
}

#[cfg(test)]
mod tests {
    use super::turned_off;
    use std::ffi::OsStr;

    #[test]
    fn only_a_value_that_starts_with_zero_turns_whole_views_off() {
        assert!(turned_off(Some(OsStr::new("0"))));
        assert!(turned_off(Some(OsStr::new("00"))));
        assert!(!turned_off(Some(OsStr::new("1"))));
        assert!(!turned_off(Some(OsStr::new(""))));
        assert!(!turned_off(Some(OsStr::new("off"))));
        assert!(!turned_off(Some(OsStr::new(" 0"))));
        assert!(!turned_off(None));
    }

    /// The plugin marks its cache tag when the first byte of the raw value is 0
    /// (nvmtl_spvcache_escape_tag in plugin/nvmtl_spvcache_name.h). A value that is not UTF-8 must
    /// be read the same way here, or a module translated with the views on would be cached under
    /// the mark of the way back.
    #[cfg(unix)]
    #[test]
    fn a_value_that_is_not_utf8_is_read_by_its_first_byte_as_the_plugin_reads_it() {
        use std::os::unix::ffi::OsStrExt;
        assert!(turned_off(Some(OsStr::from_bytes(b"0\xff"))));
        assert!(!turned_off(Some(OsStr::from_bytes(b"\xff0"))));
    }
}
