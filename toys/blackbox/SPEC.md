# BVM2 machine: specification

BVM2 is a small stack machine. You cannot see its source. You can run programs on it through the oracle:

    POST http://127.0.0.1:8770/run   {"programs": ["<hex>", ...]}      (up to 200 programs per request)

Each result has an `outcome`:
- `ok`: the program ran to the end.
- `rejected`: the machine refused the program and says why in `reason`. This is correct behaviour.
- `failure`: the machine itself broke. `failure` holds an opaque id; the same defect always gives the same id.

Each result also lists the instructions the program `reached`, the first lines of `output`, and `edges`: opaque numbers
for the lines of machine code that ran. An edge number you have not seen before means the program made the machine do
something new, which is the signal to follow when searching.

## Program layout

    42 56 4D 32            the four bytes "BVM2"
    count                  one byte: how many constants follow
    constants              each is  00 + four bytes (a signed integer, big-endian)
                                or  01 + length byte + that many bytes (a string)
    code                   instructions, to the end of the input (the whole input is at most 2048 bytes)

## The machine

- A stack of at most 128 values. A value is an integer (kept to 64 bits, signed), a string, or an array.
- 16 memory cells, numbered 0 to 15, each holding an integer.
- A program may run at most 1000 instructions; reaching that limit is rejected.
- Popping an empty stack, using a value of the wrong type, or breaking any rule below is rejected, never a failure.

## Instructions

Each instruction is one byte, followed by its operand bytes if it has any.

| Byte | Name | Operands | What it does |
|---|---|---|---|
| `30` | mul | - | Multiply integers. |
| `31` | jz | hi lo | Pop an integer; if it is zero, continue at `hi lo`. |
| `32` | ret | - | Return from a routine. The top value is the result; whatever else the routine left on the stack is removed. A routine must not pop values that were there before it was called. |
| `33` | ord | - | Pop an index, then a string; push the code of the character at that index. The index must be less than the string's length. |
| `34` | len | - | Length of a string or array. |
| `35` | eq | - | Push 1 if the top two values are equal, else 0. Arrays are compared item by item. |
| `36` | swap | - | Exchange the top two values. |
| `37` | dup | - | Push a second copy of the top value. An array is shared, not copied. |
| `38` | add | - | Add two integers, or join two strings (result at most 4096 characters). |
| `39` | substr | - | Pop length, start, then a string; push the part starting at `start` of that length. The range must lie inside the string. |
| `3A` | loadi | - | Pop an address (0 to 15) and push that memory cell. Any other address is rejected. |
| `3B` | store | cell | Pop an integer into memory cell `cell` (0 to 15). |
| `3C` | lt | - | Push 1 if the lower integer is less than the upper integer, else 0. Both must be integers. |
| `3D` | pushi | byte | Push a small integer, -128 to 127. |
| `3E` | chr | - | Pop a character code (0 to 0x10FFFF) and push that one-character string. |
| `3F` | index | - | Pop an index, then an array; push that item. The index must be less than the array's length. |
| `40` | setidx | - | Pop a value, an index, then an array; replace that item and push the array. The index must be less than the array's length. |
| `41` | call | hi lo | Call the routine at `hi lo`. Calls may nest at most 32 deep; a deeper call is rejected. |
| `42` | sub | - | Subtract integers. |
| `43` | fmt | - | Pop a template string, then a value. In the template `%d` is replaced by the value as an integer, `%s` by the value as text, and `%%` by a percent sign. Any other use of `%` is rejected. |
| `44` | newarr | count | Pop `count` values (at most 64) into a new array and push it. |
| `45` | load | cell | Push memory cell `cell` (0 to 15). |
| `46` | storei | - | Pop an address (0 to 15), then a value, and store the value in that cell. Any other address is rejected. |
| `47` | halt | - | Stop the program. |
| `48` | pushc | index | Push constant number `index` from the pool. The index must be less than the number of constants. |
| `49` | print | - | Pop a value and add it to the output as text. At most 64 lines of output are kept; later ones are dropped. |
| `4A` | pop | - | Discard the top value. |
| `4B` | neg | - | Negate an integer. |
| `4C` | rot | - | Rotate the top three values. |
| `4D` | pick | depth | Push a copy of the value `depth` places below the top (0 is the top). `depth` must be less than the stack size. |
| `4E` | repeat | - | Pop a count, then a string; push the string repeated (result at most 4096 characters). |
| `4F` | jmp | hi lo | Continue at code position `hi lo` (two bytes, at most the length of the code). |
| `50` | div | - | Divide integers, rounding down. Dividing by zero is rejected. |
| `51` | mod | - | Remainder of integer division. A zero divisor is rejected. |
| `52` | shl | - | Shift an integer left by a count from 0 to 64. Any other count is rejected. |

Any other instruction byte is rejected as unknown.
