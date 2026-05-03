from rest_framework import serializers
from .models import TeamMember, AssignmentRule


class TeamMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamMember
        fields = "__all__"


class AssignmentRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssignmentRule
        fields = "__all__"