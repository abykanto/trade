# Whipsaw Recovery Trailing Stop Logic

## Objective

When a Trade Idea incurs multiple small losses due to whipsaw exits and re-entries, the next successful trade must first recover those accumulated losses before attempting to maximize profit.

---

## Definitions

```text
Consumed Risk
=
Sum of all realized losses from previous attempts
within the same Trade Idea
```

Example:

```text
Attempt #1 = -1.50

Attempt #2 = -0.80

Attempt #3 = -0.70
```

```text
Consumed Risk = 3.00
```

---

## Recovery Rule

When the currently open trade reaches a floating profit greater than the Consumed Risk:

```text
Current Profit > Consumed Risk
```

the system must move the stop loss to secure at least the Consumed Risk amount.

---

## Example

```text
Consumed Risk = 3.00

Current Profit = 4.50
```

Move trailing stop so that:

```text
Minimum Guaranteed Profit = 3.00
```

If the market reverses:

```text
Net Result For Entire Trade Idea

= +3.00 - 3.00

= Break Even
```

The Trade Idea has fully recovered all previous whipsaw losses.

---

## Progressive Profit Protection

After recovery is achieved:

```text
Consumed Risk = 3.00

Current Profit = 8.00
```

The trailing stop should continue advancing.

Example:

```text
Profit = 8.00
Secure = 5.00

Profit = 12.00
Secure = 8.00

Profit = 16.00
Secure = 12.00
```

---

## Priority

Trailing stop logic priority:

```text
1. Recover Consumed Risk

2. Protect Net Profit

3. Maximize Trend Capture

4. Exit at Final TP if reached
```

---

## Core Principle

A Trade Idea should never successfully reach meaningful profit while leaving previously incurred whipsaw losses unrecovered.

The first responsibility of the trailing stop is to recover the cumulative losses of the Trade Idea. Only after those losses are recovered should the system focus on maximizing profit.
