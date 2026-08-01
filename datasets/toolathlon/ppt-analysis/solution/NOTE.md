# Compiler Principles — Symbol Tables (Notes for the Missed Lecture)

## Overview

This note summarizes the Compile.pptx lecture on **symbol tables** in a
compiler, and explains the homework in HW.pdf. A symbol table maps
identifiers to their bindings (types, values, scopes). The two central
operations are **insert** (add a new binding) and **lookup** (find the most
recent binding for a key). The lecture contrasts the **imperative style**
and the **functional style** of maintaining environments.

## Imperative style vs. functional style

- **Imperative style**: there is a single global table that is destructively
  updated. When a scope ends, the table must be restored by undoing the
  insertions (for example, by popping entries back to a mark). This approach
  is efficient in memory because only one table exists, but the old
  environment is destroyed by each update.
- **Functional style**: entering a scope creates a *new* environment while
  the old one is kept intact, so both σ and σ' exist at the same time. The
  advantage is that older environments remain available (useful for
  persistence); the drawback is potentially more allocation. Compared to the
  imperative style, the functional style never destroys an environment,
  therefore restoring an outer scope is trivial — you simply keep using the
  old table.

Because hash tables with external chaining insert new bindings at the front
of a bucket list, the imperative implementation can hide an older binding
and later restore it by a simple pop. This means the same bucket list acts
like a stack per key, and the algorithm has O(1) expected complexity for
both insert and lookup, which gives good efficiency and performance.

## Hash table implementation (imperative)

The lecture's C implementation of a hash table with external chaining:

```c
struct bucket { string key; void *binding; struct bucket *next; };
#define SIZE 109
struct bucket *table[SIZE];
unsigned int hash(char *s0)
{ unsigned int h=0; char *s;
  for(s=s0; *s; s++)
    h=h*65599 + *s;
  return h;
}
struct bucket *Bucket (string key, void *binding, struct bucket *next) {
  struct bucket *b=checked_malloc(sizeof(*b));
  b->key = key; b->binding = binding; b->next = next;
  return b;
}
```

The insert, lookup and pop operations:

```c
void insert(string key, void *binding) {
  int index=hash(key)%SIZE;
  table[index]=Bucket(key, binding, table[index]);
}

void *lookup(string key) {
  int index=hash(key)%SIZE
  struct bucket *b;
  for (b = table[index]; b; b=b->next)
    if (0==strcmp(b->key,key))
      return b->binding;
  return NULL;
}

void pop(string key) {
  int index=hash(key)%SIZE
  table[index]=table[index].next;
}
```

For example, `insert("a", b1)` prepends a new bucket, so a later
`lookup("a")` finds the newest binding first; `pop("a")` restores the
previous binding. The hash function multiplies by 65599 — a prime chosen
for a good distribution — which is a classic implementation technique.

## Symbol table interface (symbol.h)

The symbol module interns strings into symbols so that comparison is a
pointer comparison instead of strcmp — an efficiency win:

```c
typedef struct S_symbol_ *S_symbol;
S_symbol S_symbol (string);
string S_name(S_symbol);

typedef struct TAB_table_ *S_table;
S_table S_empty( void);
void S_enter( S_table t,S_symbol sym, void *value);
void *S_look( S_table t, S_symbol sym);
void S_beginScope( S_table t);
void S_endScope( S_table t);
```

Making symbols (interning) works like this:

```c
static S_symbol mksymbol (string name , S_symbol next) {
  S_symbol s = checked_malloc(sizeof(*s));
  s->name = name; s->next = next;
  return s;
}

S_symbol S_symbol (string name) {
	int index = hash(name)%SIZE;
	S_symbol syms = hashtable[index], sym;
	for ( sym = syms; sym; sym = sym->next)
	  if (0 == strcmp(sym->name, name)) return sym;
	sym = mksymbol(name,syms);
	hashtable[index] = sym;
   return sym;
}

string S_name (S_symbol sym) {
  return sym->name;
}
```

The symbol-table operations delegate to the underlying TAB table:

```c
// make a new S_Table
S_table S_empty(void) {
  return TAB_empty();
}
// insert a binding
void S_enter(S_table t, S_symbol sym, void *value){
  TAB_enter(t,sym,value);
}
// look up a symbol
void *S_look(S_table t, S_symbol sym) {
  return TAB_look(t,sym);
}
```

## Scope management (beginScope / endScope)

Scopes are handled with a special mark symbol. `S_beginScope` pushes the
mark; `S_endScope` pops bindings until the mark is reached — this is the
undo strategy of the imperative approach:

```c
static struct S_symbol_ marksym = { "<mark>", 0 };

void S_beginScope ( S_table t) {
  S_enter(t, &marksym, NULL);
}

void S_endScope( S_table t) {
  S_symbol s;
  do
    s= TAB_pop(t);
  while (s != &marksym);
}
```

Internally the TAB table keeps an auxiliary stack (`top` and `prevtop`
links) so it knows what to pop:

```c
struct TAB_table_ {
  binder table[TABSIZE];
  void *top;
};

t->table[index] = Binder(key, value,t->table[index], t->top);

static binder Binder(void *key, void *value, binder next, void *prevtop) {
  binder b = checked_malloc(sizeof(*b));
  b->key = key; b->value=value; b->next=next;
  b->prevtop = prevtop;
  return b;
}
```

## Homework explanation (HW.pdf)

The homework asks which identifiers are visible at each print statement of
a Tiger program, i.e. which binding is found by lookup in the symbol table
at that point.

Question code (Tiger function from the assignment):

```tiger
function f(a:int,b:int,c:int)=
(print_int (a+c);
let var j:= a+b
var a:= "hello"
in print(a); print_int(j)
end;
print_int(b)
)
```

Answer / solution: at `print_int(a+c)` the parameter `a` (an integer type)
is visible; inside the `let`, the new declaration `var a := "hello"` hides
the parameter, so `print(a)` prints the string binding — the correct answer
is that `a` has type string there, because the inner scope's insert hides
the outer binding. After `end`, the scope is popped, so `print_int(b)` sees
the original integer parameters again. This is exactly `S_beginScope` /
`S_endScope` behavior.

The related example about scoping in different languages (Java packages vs
ML structures) from the slides:

```java
package M;
class E {
static int a = 5;
}
class N {
static int b = 10;
static int a = E.a + b;
}
class D {
static int d = E.a + N.a;
}
```

```sml
structure M = struct
   structure E = struct
      val a = 5;
   end
   structure N = struct
      val b = 10
      val a = E.a + b
   end
   structure D = struct
      val d = E.a + N.a
   end
end
```

In Java the declarations in a package are mutually recursive and order does
not matter, whereas in ML the structure bindings are sequential, such as
`N.a = E.a + b` which needs `E` already bound. Therefore `D.d = 5 + 15 =
20` in both, but the analysis (the environment handling) differs.
