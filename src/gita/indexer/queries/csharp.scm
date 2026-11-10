; Type declarations
(class_declaration) @class.body
(struct_declaration) @class.body
(interface_declaration) @class.body
(record_declaration) @class.body
(enum_declaration) @class.body

; Members with code, and local functions
(method_declaration) @function.body
(constructor_declaration) @function.body
(destructor_declaration) @function.body
(local_function_statement) @function.body

; using directives name namespaces, not files: stored, left unresolved
(using_directive) @import
