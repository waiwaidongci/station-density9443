from enum import Enum
class Role(str,Enum):
    admin='admin'; analyst='analyst'; viewer='viewer'
class RunState(str,Enum):
    queued='queued'; running='running'; completed='completed'; failed='failed'
class ReviewState(str,Enum):
    draft='draft'; approved='approved'; purged='purged'
class PurgeState(str,Enum):
    accepted='accepted'; processing='processing'; completed='completed'; failed='failed'
