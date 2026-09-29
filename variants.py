"""Fresh numbers for math cards.

A math card can get a *template*: its question and answer with the numbers
replaced by named blanks, plus the formulas that compute the answer. Each time
the card comes up, the app picks new numbers within realistic ranges and works
the answer out itself, so the reference answer is always exact.

    question: "Equity value ${E}M, debt ${D}M, cash ${C}M. What's enterprise value?"
    answer:   "EV = {E} + {D} - {C} = {EV:,.0f}"
    variables: E in 300..900 (step 10) ...     derived: EV = E + D - C

Claude drafts templates once; an automatic check runs every draft with the card's
ORIGINAL numbers and rejects it unless it reproduces the ORIGINAL answer; then I
approve each one. Claude's formulas are never run as Python: they go through the
small calculator below, which only understands arithmetic and a few math functions.
"""
import ast
import json
import math
import operator
import random
import re
from datetime import datetime, timezone

from pydantic import BaseModel

import grader


class TemplateError(Exception):
    """A template that can't be used, with the reason in plain English."""


# ------------------------------------------------------------------ safe calculator

def _power(base, exponent):
    if abs(exponent) > 400 or abs(base) > 1e9:
        raise TemplateError("a power is too large")
    return operator.pow(base, exponent)


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: _power, ast.Mod: operator.mod}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_COMPARE = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne}
def annuity_pv(rate, periods):
    """Present value of 1 per period for `periods` periods at `rate` (a decimal)."""
    return periods if rate == 0 else (1 - (1 + rate) ** -periods) / rate


def annuity_fv(rate, periods):
    """Future value of 1 per period for `periods` periods at `rate` (a decimal)."""
    return periods if rate == 0 else ((1 + rate) ** periods - 1) / rate


FUNCTIONS = {"round": round, "min": min, "max": max, "abs": abs,
             "sqrt": math.sqrt, "log": math.log, "ln": math.log, "exp": math.exp,
             "annuity_pv": annuity_pv, "annuity_fv": annuity_fv}
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")


def evaluate(expression, names):
    """Work out an arithmetic expression like "E + D - C" or "CF / (r/100 - g/100)".

    Only numbers, the given names, + - * / ** %, comparisons, and/or, and the
    functions in FUNCTIONS are allowed. Anything else raises TemplateError.
    """
    if not isinstance(expression, str) or len(expression) > 400:
        raise TemplateError("a formula is missing or too long")
    # Claude sometimes writes {r} for r inside formulas; the braces add nothing.
    expression = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", r"\1", expression)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise TemplateError(f"formula isn't valid: {expression}")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            raise TemplateError(f"unknown name '{node.id}' in {expression}")
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Compare) and all(type(op) in _COMPARE for op in node.ops):
            left = ev(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = ev(right_node)
                if not _COMPARE[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            values = [ev(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in FUNCTIONS and not node.keywords):
            return FUNCTIONS[node.func.id](*[ev(a) for a in node.args])
        raise TemplateError(f"'{ast.unparse(node)}' isn't allowed in a formula")

    try:
        result = ev(tree)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as err:
        raise TemplateError(f"{expression} can't be calculated ({err.__class__.__name__})")
    if isinstance(result, complex) or (isinstance(result, float) and not math.isfinite(result)):
        raise TemplateError(f"{expression} doesn't give a real number")
    return result


# ------------------------------------------------------------------ filling in blanks

INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")  # zero-width characters

# A blank is {name}, {name:format} or a small calculation like {(r - g)/100:.2f},
# which goes through the same safe calculator as the formulas.
PLACEHOLDER = re.compile(r"\{([^{}:]+)(?::([^{}]*))?\}")


def _format(value, spec):
    if spec:
        return format(value, spec)
    if isinstance(value, bool):
        return str(value)
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def fill(text, values):
    """Replace {name} / {name:,.1f} blanks with values. Unknown names raise TemplateError."""
    def replace(match):
        expression, spec = match.group(1).strip(), match.group(2)
        if NAME.match(expression) and expression not in values:
            raise TemplateError(f"the text uses {{{expression}}}, which isn't defined")
        value = values[expression] if expression in values else evaluate(expression, values)
        try:
            return _format(value, spec)
        except (ValueError, TypeError):
            raise TemplateError(f"bad number format '{spec}' for {name}")
    result = PLACEHOLDER.sub(replace, INVISIBLE.sub("", text))
    leftover = re.search(r"\{[^{}]*\}", result)
    if leftover:  # e.g. {E.__class__}: never evaluated, but it would read as a broken blank
        raise TemplateError(f"the text has a blank that can't be filled: {leftover.group(0)}")
    return result


# ------------------------------------------------------------------ picking numbers

def validate(spec):
    """Basic sanity checks on a template's structure. Raises TemplateError."""
    names = set()
    for v in spec["variables"]:
        if not NAME.match(v["name"]) or v["name"] in FUNCTIONS or v["name"] in names:
            raise TemplateError(f"bad or repeated variable name '{v['name']}'")
        if not v["step"] > 0 or v["min"] > v["max"] or (v["max"] - v["min"]) / v["step"] > 1e6:
            raise TemplateError(f"bad range for {v['name']}")
        names.add(v["name"])
    for d in spec["derived"]:
        if not NAME.match(d["name"]) or d["name"] in FUNCTIONS or d["name"] in names:
            raise TemplateError(f"bad or repeated result name '{d['name']}'")
        names.add(d["name"])
    if not spec["variables"]:
        raise TemplateError("no numbers to vary")


def compute(spec, inputs):
    """All values (inputs + derived results, in order) for one set of inputs."""
    values = dict(inputs)
    for d in spec["derived"]:
        values[d["name"]] = evaluate(d["formula"], values)
    for rule in spec.get("constraints", []):
        if not evaluate(rule, values):
            raise TemplateError(f"these numbers break the rule {rule}")
    return values


def _pick(v, rng):
    steps = int(round((v["max"] - v["min"]) / v["step"]))
    value = round(v["min"] + v["step"] * rng.randint(0, steps), 10)
    whole = all(float(x).is_integer() for x in (v["min"], v["step"]))
    return int(value) if whole else value


def sample(spec, seed):
    """A random, valid set of values for this template, repeatable from `seed`."""
    rng = random.Random(seed)
    for _ in range(200):
        try:
            return compute(spec, {v["name"]: _pick(v, rng) for v in spec["variables"]})
        except TemplateError:
            continue  # e.g. growth rate above discount rate: pick again
    raise TemplateError("random numbers keep giving impossible values")


def original_values(spec):
    return compute(spec, {v["name"]: v["original"] for v in spec["variables"]})


# ------------------------------------------------------------------ the automatic check

NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def numbers_in(text):
    return [float(n.replace(",", "")) for n in NUMBER.findall(text or "")]


def _close(a, b):
    return abs(a - b) <= max(0.051, 0.01 * abs(b))  # cards round their answers


def _appears(value, numbers, units=False):
    """Is `value` in the text, allowing for rounding and for 10.5% vs 0.105?
    With units=True, also accept thousands/millions/billions ("24M" for 24,000,000)."""
    scales = (1, 100, 0.01) + ((1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9) if units else ())
    return any(_close(value / k, n) for k in scales for n in numbers)


def check(spec, card):
    """Does the template reproduce the ORIGINAL card with the original numbers?
    Returns (ok, [notes]). Every note is a reason the template was rejected."""
    problems = []
    try:
        validate(spec)
        values = original_values(spec)
        question = fill(spec["question"], values)
        fill(spec["answer"], values)
    except TemplateError as err:
        return False, [str(err)]

    # 1. The template's formulas give the same results as the card's own answer.
    if not spec["expected"]:
        problems.append("it names no result to check against the original answer")
    answer_numbers = numbers_in(card["answer"])
    for e in spec["expected"]:
        if e["name"] not in values:
            problems.append(f"expected result '{e['name']}' isn't calculated")
        elif not _close(values[e["name"]], e["value"]):
            problems.append(f"{e['name']} comes out as {values[e['name']]:,.4g}, "
                            f"but the card's answer says {e['value']:,.4g}")
        elif not _appears(e["value"], answer_numbers, units=True):
            problems.append(f"{e['value']:,.4g} doesn't appear in the card's answer")

    # 2. With the original numbers, the question reads like the original.
    rebuilt = numbers_in(question)
    missing = [n for n in numbers_in(card["question"]) if not _appears(n, rebuilt, units=True)]
    if missing:
        problems.append("the rebuilt question is missing the number(s) "
                        + ", ".join(f"{n:,g}" for n in missing[:4]))

    # 3. Random numbers work too, and never produce garbled numbers like "1.7.5"
    #    (a blank glued onto a digit, e.g. "1.{r}" written for 1.10).
    try:
        for seed in range(25):
            s = sample(spec, seed)
            for text in (fill(spec["question"], s), fill(spec["answer"], s),
                         fill(spec["question"], values), fill(spec["answer"], values)):
                garbled = GARBLED.search(text)
                if garbled:
                    raise TemplateError(f"a number comes out garbled: '{garbled.group(0)}'")
    except TemplateError as err:
        problems.append(f"with random numbers: {err}")

    # 4. The working must actually change with the numbers: a figure from the original
    #    answer that stays identical across random versions (and isn't fixed text in the
    #    question, or a common constant like 12 months or 360 days) was left behind.
    if not problems:
        samples = [sample(spec, seed) for seed in (101, 202, 303, 404, 505, 606)]
        # abs(): a minus sign is usually an exponent, as in (1 + r)^-2
        sizes = lambda text: {abs(n) for n in numbers_in(text)}
        in_every = lambda texts: set.intersection(*(sizes(t) for t in texts))
        answer_constants = in_every([fill(spec["answer"], s) for s in samples])
        question_constants = in_every([fill(spec["question"], s) for s in samples])
        leftovers = sorted(n for n in answer_constants - question_constants - COMMON_CONSTANTS
                           if n in sizes(card["answer"]))
        if leftovers:
            problems.append("the working keeps the original number(s) "
                            + ", ".join(f"{n:g}" for n in leftovers[:4])
                            + " whatever the new inputs are")
    return not problems, problems


COMMON_CONSTANTS = {float(n) for n in (*range(0, 13), 24, 30, 52, 70, 72, 100, 360, 365, 1000)}


# "1.7.5" (a blank glued onto digits) or "10,000,000M" (millions that kept an "M"; "$1,700M" is fine).
GARBLED = re.compile(r"\d\.\d+\.\d|\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?\s?(?:K|M|MM|B|bn)\b")


# ------------------------------------------------------------------ which cards are math cards

_CALCULATION = re.compile(r"[=×x÷/*+−-]\s*\$?\d|≈")


def is_math_card(card):
    """Rough test: the question gives 2+ numbers and the answer shows a calculation."""
    return len(NUMBER.findall(card["question"])) >= 2 and bool(_CALCULATION.search(card["answer"]))


def candidate_counts(conn):
    counts = {}
    for card in conn.execute("SELECT deck, question, answer FROM cards"):
        if is_math_card(card):
            counts[card["deck"]] = counts.get(card["deck"], 0) + 1
    return counts


# ------------------------------------------------------------------ drafting with Claude

DRAFT_PROMPT = """\
You turn a finance flashcard into a template so the app can ask it with fresh numbers \
each time. The app computes the answer itself from your formulas.

Return:
- suitable: false if the numbers can't sensibly change (the specific numbers ARE the \
point, it's a data set to interpret, a true/false, or there is no calculation). Then \
leave the other lists empty.
- variables: each input number in the question. name (short, letters/digits/_), \
original (exactly as in the question), min/max/step for realistic random values \
(roughly 50%-150% of the original, rounded to a sensible step). Write percentages as \
plain numbers (10 for 10%) and divide by 100 inside formulas.
- derived: each result, in the order to calculate it, as a Python-style expression \
using the variable names and earlier results, WITHOUT braces (write r, not {r}). \
Use ** for powers. Allowed functions: round, min, max, abs, sqrt, log, exp, and for \
level cash-flow streams annuity_pv(rate, n) = (1 - (1+rate)**-n)/rate and \
annuity_fv(rate, n) = ((1+rate)**n - 1)/rate, with rate as a decimal. No loops, \
sum() or list comprehensions: use the annuity functions, or write uneven cash flows \
out term by term.
- units: a variable's original is the number exactly as written ("$8M" -> original 8, \
text "${par}M"; "$250K" -> 250, text "${price}K").
- constraints: conditions the inputs must satisfy for the problem to make sense \
(e.g. "r > g"). IMPORTANT: if the answer reaches a conclusion that depends on the \
numbers (e.g. "will convert", "accretive", "in the money", "A is more convex"), add \
constraints so every random set of numbers leads to that same conclusion (e.g. \
"conv_price < price"), because the conclusion's wording doesn't change. Can be empty.
- Never glue a blank onto digits: write "(1 + {r}/100)" or "{growth_factor:.2f}", \
not "1.{r}".
- question: the original question word for word, with each input number replaced by \
{name}. Keep units and symbols outside the braces ("${E}M", "{r}%"), and keep every \
other number (dates, totals, labels) exactly as written: a total that follows from \
the inputs should be a derived value shown with its own {name}.
- Braces hold a name with an optional format ({EV} or {EV:,.1f}). A short \
calculation is allowed too ({(r - g)/100:.2f}), but prefer a derived value.
- answer: the original answer's working, with numbers replaced by {name} or \
{name:,.1f} (Python format spec) so it shows the steps with the new numbers.
- expected: the key results as they appear in the ORIGINAL answer: which derived name \
should equal which value. The app checks your formulas against these, so copy the \
values exactly from the answer text.\
"""


class Variable(BaseModel):
    name: str
    original: float
    min: float
    max: float
    step: float


class Derived(BaseModel):
    name: str
    formula: str


class Expected(BaseModel):
    name: str
    value: float


class TemplateDraft(BaseModel):
    suitable: bool
    reason: str
    variables: list[Variable]
    derived: list[Derived]
    constraints: list[str]
    question: str
    answer: str
    expected: list[Expected]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def draft_template(conn, card):
    """Ask Claude for a template for one card, run the check, save it as a draft.
    Returns the status saved: 'draft' (passed the check), 'failed' or 'unsuitable'."""
    response = grader.call_claude(
        system=DRAFT_PROMPT,
        prompt=f"<question>\n{card['question']}\n</question>\n\n<answer>\n{card['answer']}\n</answer>",
        output_format=TemplateDraft, max_tokens=4000)
    grader.record_usage(conn, "draft_template", response.usage.input_tokens,
                        response.usage.output_tokens)
    draft = response.parsed_output
    spec = draft.model_dump(exclude={"suitable", "reason"})
    if not draft.suitable:
        status, notes = "unsuitable", [draft.reason]
    else:
        ok, notes = check(spec, card)
        status = "draft" if ok else "failed"
    conn.execute(
        "INSERT INTO card_templates (card_id, status, spec, notes, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET status = excluded.status, spec = excluded.spec, "
        "notes = excluded.notes, updated_at = excluded.updated_at",
        (card["id"], status, json.dumps(spec), json.dumps(notes), _now()),
    )
    conn.commit()
    return status


def set_status(conn, card_id, status):
    conn.execute("UPDATE card_templates SET status = ?, updated_at = ? WHERE card_id = ?",
                 (status, _now(), card_id))
    conn.commit()


def templates_for_deck(conn, deck):
    """Every math card in a deck, with its template (if any)."""
    rows = []
    for card in conn.execute("SELECT * FROM cards WHERE deck = ? ORDER BY id", (deck,)):
        if not is_math_card(card):
            continue
        t = conn.execute("SELECT * FROM card_templates WHERE card_id = ?", (card["id"],)).fetchone()
        rows.append({"card": card, "template": t,
                     "spec": json.loads(t["spec"]) if t else None,
                     "notes": json.loads(t["notes"]) if t else []})
    return rows


# ------------------------------------------------------------------ using templates in study

def present(conn, card, seed=None):
    """The card as it should be shown now: with fresh numbers if it has an approved
    template. Returns (card_dict, seed); seed is None for ordinary cards. Passing the
    same seed again gives the same numbers (so grading matches what I saw)."""
    row = conn.execute("SELECT spec FROM card_templates WHERE card_id = ? AND status = 'approved'",
                       (card["id"],)).fetchone()
    shown = dict(card)
    if row is None:
        return shown, None
    spec = json.loads(row["spec"])
    seed = seed if seed is not None else random.randrange(1, 2**31)
    try:
        values = sample(spec, seed)
        shown["question"], shown["answer"] = fill(spec["question"], values), fill(spec["answer"], values)
        shown["randomised"] = True
    except TemplateError:
        return dict(card), None  # never block studying: fall back to the original card
    return shown, seed


def preview(spec, seed):
    """(question, answer) with random numbers, for the approval page."""
    values = sample(spec, seed)
    return fill(spec["question"], values), fill(spec["answer"], values)
