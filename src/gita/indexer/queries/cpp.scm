; Function definitions: free functions, methods defined inside a class body,
; and out-of-line definitions (void Player::Update(...) { ... }). Names,
; owning class and signature are worked out from the declarator in Python.
(function_definition) @function.body

; Types with a body. Forward declarations (class Foo;) have none.
(class_specifier body: (field_declaration_list)) @class.body
(struct_specifier body: (field_declaration_list)) @class.body
(union_specifier body: (field_declaration_list)) @class.body
(enum_specifier body: (enumerator_list)) @class.body

; #include "..." and #include <...>
(preproc_include) @import
