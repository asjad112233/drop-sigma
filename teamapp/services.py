from .models import TeamMember, AssignmentRule


def get_rule_type_for_order(order):
    payment_status = (order.payment_status or "").lower()
    tracking_number = order.tracking_number

    if "failed" in payment_status or "cancelled" in payment_status:
        return "failed_payment"

    if "refund" in payment_status or "dispute" in payment_status:
        return "refund_dispute"

    if not tracking_number:
        return "tracking_missing"

    return "new_order"


def auto_assign_order(order):
    rule_type = get_rule_type_for_order(order)

    print("Order:", order.id, "Rule:", rule_type)

    rule = AssignmentRule.objects.filter(
        rule_type=rule_type,
        is_active=True
    ).first()

    print("Found rule:", rule)

    if not rule:
        return None

    member = TeamMember.objects.filter(
        role=rule.assign_to_role,
        status="available",
        is_active=True,
        workload__lt=90
    ).order_by("workload").first()

    print("Found member:", member)

    if not member:
        return None

    order.assigned_to = member
    order.save()

    member.workload = min(member.workload + 5, 100)
    member.save()

    return member