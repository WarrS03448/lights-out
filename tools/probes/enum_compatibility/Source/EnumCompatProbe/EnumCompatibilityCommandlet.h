#pragma once
#include "Commandlets/Commandlet.h"
#include "EnumCompatibilityCommandlet.generated.h"

UCLASS()
class UEnumCompatibilityCommandlet : public UCommandlet {
    GENERATED_BODY()
public:
    virtual int32 Main(const FString& Params) override;
};
