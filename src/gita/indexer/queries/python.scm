; Function definitions — captures both sync and async functions.
; The Python grammar represents `async def` as a function_definition whose
; first child is an `async` keyword token, so one query catches both.
(function_definition
  name: (identifier) @function.name) @function.body

; Class definitions
(class_definition
  name: (identifier) @class.name) @class.body

; Imports (both forms)
(import_statement) @import
(import_from_statement) @import
