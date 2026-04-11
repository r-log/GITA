; Top-level function declarations (sync and async)
(function_declaration
  name: (identifier) @function.name) @function.body

; Class declarations — TS uses type_identifier for the class name
(class_declaration
  name: (type_identifier) @class.name) @class.body

; Interface declarations
(interface_declaration
  name: (type_identifier) @interface.name) @interface.body

; Method definitions (inside class bodies)
(method_definition
  name: (property_identifier) @method.name) @method.body

; Arrow function bound to a const/let/var name:
;   const square = (x) => x * x;
(variable_declarator
  name: (identifier)
  value: (arrow_function)) @arrow.decl

; Imports
(import_statement) @import
