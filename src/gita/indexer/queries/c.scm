; Same shape as cpp.scm, without the C++-only node types.
(function_definition) @function.body

(struct_specifier body: (field_declaration_list)) @class.body
(union_specifier body: (field_declaration_list)) @class.body
(enum_specifier body: (enumerator_list)) @class.body

(preproc_include) @import
