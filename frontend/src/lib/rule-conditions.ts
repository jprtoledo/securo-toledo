import type { RuleCondition, RuleConditionGroup, RuleConditionNode, RuleConditionNot } from '../types'

/** A condition node is a group when it carries its own condition list. */
export function isConditionGroup(node: RuleConditionNode): node is RuleConditionGroup {
  return Array.isArray((node as RuleConditionGroup).conditions)
}

/** A negation: `{not: node}`. */
export function isConditionNot(node: RuleConditionNode): node is RuleConditionNot {
  return !isConditionGroup(node) && typeof (node as RuleConditionNot).not === 'object'
    && (node as RuleConditionNot).not !== null
}

/** Every leaf condition of a rule, at any depth. */
export function flattenConditions(nodes: RuleConditionNode[]): RuleCondition[] {
  return nodes.flatMap(node => {
    if (isConditionGroup(node)) return flattenConditions(node.conditions)
    if (isConditionNot(node)) return flattenConditions([node.not])
    return [node]
  })
}

/** True when a rule goes beyond a flat list (groups or negations). */
export function hasConditionGroups(nodes: RuleConditionNode[]): boolean {
  return nodes.some(node => isConditionGroup(node) || isConditionNot(node))
}

/** Nesting depth of a node (a leaf is 1), as the API counts it. */
export function nodeDepth(node: RuleConditionNode): number {
  if (isConditionGroup(node)) return 1 + Math.max(0, ...node.conditions.map(nodeDepth))
  if (isConditionNot(node)) return 1 + nodeDepth(node.not)
  return 1
}

export const MAX_CONDITION_DEPTH = 5
