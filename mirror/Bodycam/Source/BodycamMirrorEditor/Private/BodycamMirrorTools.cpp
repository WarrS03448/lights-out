#include "BodycamMirrorTools.h"
#include "Engine/Blueprint.h"
#include "Engine/UserDefinedEnum.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraphUtilities.h"
#include "EdGraphSchema_K2.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/EnumEditorUtils.h"
#include "UObject/UnrealType.h"
#include "K2Node_Event.h"
#include "K2Node_CallFunction.h"
#include "Kismet/KismetSystemLibrary.h"
#include "GameFramework/Actor.h"
#include "SubobjectDataSubsystem.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SCS_Node.h"
#include "Components/WidgetComponent.h"
#include "GameplayEffect.h"
#include "GameplayEffectComponents/TargetTagsGameplayEffectComponent.h"
#include "AttributeSet.h"
#include "GameplayTagContainer.h"
#include "GameplayTagsManager.h"
#include "Engine/Engine.h"

UEdGraph* UBodycamMirrorTools::FindGraph(UBlueprint* Blueprint, const FString& GraphName, bool bCreateFunctionGraph) {
    if (!Blueprint) return nullptr;
    if (GraphName.Equals(TEXT("EventGraph"), ESearchCase::IgnoreCase)) {
        if (Blueprint->UbergraphPages.Num() > 0) return Blueprint->UbergraphPages[0];
        UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(Blueprint, UEdGraphSchema_K2::GN_EventGraph, UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
        FBlueprintEditorUtils::AddUbergraphPage(Blueprint, NewGraph);
        return NewGraph;
    }
    const FName Name(*GraphName);
    for (UEdGraph* G : Blueprint->FunctionGraphs) if (G && G->GetFName() == Name) return G;
    for (UEdGraph* G : Blueprint->UbergraphPages) if (G && G->GetFName() == Name) return G;
    if (!bCreateFunctionGraph) return nullptr;
    UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(Blueprint, Name, UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
    FBlueprintEditorUtils::AddFunctionGraph<UClass>(Blueprint, NewGraph, /*bIsUserCreated*/ true, nullptr);
    return NewGraph;
}

FString UBodycamMirrorTools::ImportGraphText(UBlueprint* Blueprint, const FString& GraphName, const FString& Text, bool bReconstructNodes) {
    UEdGraph* Graph = FindGraph(Blueprint, GraphName, true);
    if (!Graph) return TEXT("ERROR: no graph");
    TSet<UEdGraphNode*> Imported;
    FEdGraphUtilities::ImportNodesFromText(Graph, Text, Imported);
    FString Report = FString::Printf(TEXT("imported %d node(s) into %s:"), Imported.Num(), *Graph->GetName());
    for (UEdGraphNode* Node : Imported) {
        if (!Node) continue;
        Node->CreateNewGuid();
        Node->PostPasteNode();
        Report += FString::Printf(TEXT(" %s"), *Node->GetClass()->GetName());
    }
    if (bReconstructNodes) {
        for (UEdGraphNode* Node : Imported) if (Node) Node->ReconstructNode();
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return Report;
}

FString UBodycamMirrorTools::CompileAndReport(UBlueprint* Blueprint) {
    if (!Blueprint) return TEXT("ERROR: null blueprint");
    FCompilerResultsLog Results;
    Results.bSilentMode = true;
    FKismetEditorUtilities::CompileBlueprint(Blueprint, EBlueprintCompileOptions::None, &Results);
    FString Out;
    for (const TSharedRef<FTokenizedMessage>& Msg : Results.Messages) {
        const EMessageSeverity::Type Sev = Msg->GetSeverity();
        if (Sev == EMessageSeverity::Error || Sev == EMessageSeverity::Warning || Sev == EMessageSeverity::PerformanceWarning) {
            Out += (Sev == EMessageSeverity::Error ? TEXT("ERROR: ") : TEXT("WARNING: ")) + Msg->ToText().ToString() + TEXT("\n");
        }
    }
    if (Blueprint->Status == BS_Error) Out += TEXT("STATUS: BS_Error\n");
    return Out;
}

FString UBodycamMirrorTools::DescribeClassLayout(UClass* Class, bool bIncludeSuper) {
    if (!Class) return TEXT("ERROR: null class");
    FString Out; int32 Index = 0;
    for (TFieldIterator<FProperty> It(Class, bIncludeSuper ? EFieldIteratorFlags::IncludeSuper : EFieldIteratorFlags::ExcludeSuper); It; ++It) {
        const FProperty* P = *It;
        Out += FString::Printf(TEXT("#%d %s %s [%s] flags=%llx\n"), Index++, *P->GetClass()->GetName(), *P->GetName(),
                               *P->GetOwnerStruct()->GetName(), (unsigned long long)P->GetPropertyFlags());
    }
    return Out;
}

bool UBodycamMirrorTools::SetUserEnumerators(UUserDefinedEnum* Enum, const TArray<FString>& Names) {
    if (!Enum) return false;
    // a fresh UserDefinedEnum has one enumerator; add until we have Names.Num(), then rename the display names
    while (Enum->NumEnums() - 1 < Names.Num()) FEnumEditorUtils::AddNewEnumeratorForUserDefinedEnum(Enum);
    for (int32 i = 0; i < Names.Num(); ++i) {
        FEnumEditorUtils::SetEnumeratorDisplayName(Enum, i, FText::FromString(Names[i]));
    }
    return true;
}

FString UBodycamMirrorTools::AddBeginPlayPrint(UBlueprint* Blueprint, const FString& Text) {
    UEdGraph* Graph = FindGraph(Blueprint, TEXT("EventGraph"), true);
    if (!Graph) return TEXT("ERROR: no event graph");
    const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
    UK2Node_Event* EventNode = NewObject<UK2Node_Event>(Graph);
    EventNode->EventReference.SetExternalMember(FName(TEXT("ReceiveBeginPlay")), AActor::StaticClass());
    EventNode->bOverrideFunction = true;
    Graph->AddNode(EventNode, false, false);
    EventNode->CreateNewGuid();
    EventNode->PostPlacedNewNode();
    EventNode->AllocateDefaultPins();
    EventNode->NodePosX = 0; EventNode->NodePosY = 0;

    UK2Node_CallFunction* Call = NewObject<UK2Node_CallFunction>(Graph);
    Call->SetFromFunction(UKismetSystemLibrary::StaticClass()->FindFunctionByName(TEXT("PrintString")));
    Graph->AddNode(Call, false, false);
    Call->CreateNewGuid();
    Call->PostPlacedNewNode();
    Call->AllocateDefaultPins();
    Call->NodePosX = 300; Call->NodePosY = 0;
    if (UEdGraphPin* InString = Call->FindPin(TEXT("InString"))) InString->DefaultValue = Text;

    UEdGraphPin* Then = EventNode->FindPin(UEdGraphSchema_K2::PN_Then);
    UEdGraphPin* Exec = Call->GetExecPin();
    const bool bLinked = (Then && Exec) ? Schema->TryCreateConnection(Then, Exec) : false;
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return bLinked ? TEXT("ok") : TEXT("ERROR: could not link BeginPlay -> PrintString");
}

bool UBodycamMirrorTools::AddVariable(UBlueprint* Blueprint, const FString& Name, const FString& Category, UObject* SubObject, bool bArray) {
    if (!Blueprint) return false;
    FEdGraphPinType Type;
    const FString Cat = Category.ToLower();
    if (Cat == TEXT("bool")) Type.PinCategory = UEdGraphSchema_K2::PC_Boolean;
    else if (Cat == TEXT("byte")) Type.PinCategory = UEdGraphSchema_K2::PC_Byte;
    else if (Cat == TEXT("int")) Type.PinCategory = UEdGraphSchema_K2::PC_Int;
    else if (Cat == TEXT("int64")) Type.PinCategory = UEdGraphSchema_K2::PC_Int64;
    else if (Cat == TEXT("float") || Cat == TEXT("double") || Cat == TEXT("real")) { Type.PinCategory = UEdGraphSchema_K2::PC_Real; Type.PinSubCategory = UEdGraphSchema_K2::PC_Double; }
    else if (Cat == TEXT("name")) Type.PinCategory = UEdGraphSchema_K2::PC_Name;
    else if (Cat == TEXT("string")) Type.PinCategory = UEdGraphSchema_K2::PC_String;
    else if (Cat == TEXT("text")) Type.PinCategory = UEdGraphSchema_K2::PC_Text;
    else if (Cat == TEXT("object")) { Type.PinCategory = UEdGraphSchema_K2::PC_Object; Type.PinSubCategoryObject = SubObject ? SubObject : UObject::StaticClass(); }
    else if (Cat == TEXT("class")) { Type.PinCategory = UEdGraphSchema_K2::PC_Class; Type.PinSubCategoryObject = SubObject ? SubObject : UObject::StaticClass(); }
    else if (Cat == TEXT("struct")) { Type.PinCategory = UEdGraphSchema_K2::PC_Struct; Type.PinSubCategoryObject = SubObject; if (!SubObject) return false; }
    else return false;
    if (bArray) Type.ContainerType = EPinContainerType::Array;
    return FBlueprintEditorUtils::AddMemberVariable(Blueprint, FName(*Name), Type);
}

FString UBodycamMirrorTools::AddComponent(UBlueprint* Blueprint, UClass* ComponentClass, const FString& VariableName) {
    if (!Blueprint || !ComponentClass) return TEXT("ERROR: null arguments");
    USubobjectDataSubsystem* SDS = GEngine ? GEngine->GetEngineSubsystem<USubobjectDataSubsystem>() : nullptr;
    if (!SDS) return TEXT("ERROR: no SubobjectDataSubsystem");
    TArray<FSubobjectDataHandle> Handles;
    SDS->K2_GatherSubobjectDataForBlueprint(Blueprint, Handles);
    if (Handles.Num() == 0) return TEXT("ERROR: no root handle");
    FAddNewSubobjectParams Params;
    Params.ParentHandle = Handles[0];
    Params.NewClass = ComponentClass;
    Params.BlueprintContext = Blueprint;
    FText Fail;
    const FSubobjectDataHandle Handle = SDS->AddNewSubobject(Params, Fail);
    if (!Handle.IsValid()) return FString::Printf(TEXT("ERROR: add failed: %s"), *Fail.ToString());
    const bool bRenamed = SDS->RenameSubobject(Handle, FText::FromString(VariableName));
    FString Actual = VariableName;
    if (const FSubobjectData* Data = Handle.GetData()) Actual = Data->GetVariableName().ToString();
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return FString::Printf(TEXT("%s%s"), bRenamed ? TEXT("") : TEXT("(rename failed) "), *Actual);
}

FString UBodycamMirrorTools::SetWidgetComponentDefaults(UBlueprint* Blueprint, const FString& VariableName, UClass* WidgetClass, bool bScreenSpace) {
    // Class defaults of an SCS widget component (WidgetClass / Space are not Blueprint-settable at runtime in 5.5)
    if (!Blueprint || !Blueprint->SimpleConstructionScript) return TEXT("ERROR: no construction script");
    USCS_Node* Node = Blueprint->SimpleConstructionScript->FindSCSNode(FName(*VariableName));
    if (!Node) return TEXT("ERROR: no component named ") + VariableName;
    UWidgetComponent* Template = Cast<UWidgetComponent>(Node->ComponentTemplate);
    if (!Template) return TEXT("ERROR: not a widget component: ") + VariableName;
    Template->Modify();
    if (WidgetClass) Template->SetWidgetClass(WidgetClass);
    Template->SetWidgetSpace(bScreenSpace ? EWidgetSpace::Screen : EWidgetSpace::World);
    // like the game's BombZone marker: draw the widget at its own desired size (otherwise the 500x500 DrawSize box is centred
    // on the projected point and the dot lands 250 px up-left of the flag), DrawSize 1920x1080
    Template->SetDrawAtDesiredSize(true);
    Template->SetDrawSize(FVector2D(1920.0, 1080.0));
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return FString::Printf(TEXT("ok: %s -> %s, %s space"), *VariableName, WidgetClass ? *WidgetClass->GetName() : TEXT("(none)"), bScreenSpace ? TEXT("screen") : TEXT("world"));
}

FString UBodycamMirrorTools::SetGameplayEffectGrantedTags(UBlueprint* Blueprint, const TArray<FString>& Tags) {
    // Tags the effect grants to its target while active (UE 5.3+: the TargetTags component, not the deprecated container)
    if (!Blueprint || !Blueprint->GeneratedClass) return TEXT("ERROR: null");
    UGameplayEffect* GE = Cast<UGameplayEffect>(Blueprint->GeneratedClass->GetDefaultObject());
    if (!GE) return TEXT("ERROR: not a GameplayEffect blueprint");
    UTargetTagsGameplayEffectComponent& Comp = GE->FindOrAddComponent<UTargetTagsGameplayEffectComponent>();
    FInheritedTagContainer Container;
    FString Missing;
    for (const FString& T : Tags) {
        const FGameplayTag Tag = FGameplayTag::RequestGameplayTag(FName(*T), /*ErrorIfNotFound*/ false);
        if (!Tag.IsValid()) { Missing += T + TEXT(" "); continue; }
        Container.AddTag(Tag);
    }
    Comp.SetAndApplyTargetTagChanges(Container);
    GE->Modify();
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return Missing.IsEmpty() ? FString::Printf(TEXT("ok: %d tag(s)"), Tags.Num()) : TEXT("ERROR: unknown tag(s) ") + Missing;
}

FString UBodycamMirrorTools::AddGameplayEffectModifier(UBlueprint* Blueprint, UClass* AttributeSetClass, const FString& AttributeName, const FString& Op, float Magnitude) {
    // e.g. CharacterAttributeSet.GadgetCooldown x 2.5 -> the game's perk cooldown ability reads the attribute's CURRENT value
    // (GetFloatAttributeFromAbilitySystemComponent) when it builds the cooldown effect, so an infinite multiplicative modifier scales it
    if (!Blueprint || !Blueprint->GeneratedClass) return TEXT("ERROR: null blueprint");
    UGameplayEffect* GE = Cast<UGameplayEffect>(Blueprint->GeneratedClass->GetDefaultObject());
    if (!GE) return TEXT("ERROR: not a GameplayEffect blueprint");
    if (!AttributeSetClass || !AttributeSetClass->IsChildOf(UAttributeSet::StaticClass())) return TEXT("ERROR: AttributeSetClass is not an AttributeSet");
    FProperty* Prop = FindFProperty<FProperty>(AttributeSetClass, *AttributeName);
    if (!Prop) return FString::Printf(TEXT("ERROR: no property %s on %s"), *AttributeName, *AttributeSetClass->GetName());
    if (!FGameplayAttribute::IsGameplayAttributeDataProperty(Prop)) return FString::Printf(TEXT("ERROR: %s is not a GameplayAttributeData"), *AttributeName);
    EGameplayModOp::Type ModOp;
    if (Op == TEXT("Add")) ModOp = EGameplayModOp::Additive;
    else if (Op == TEXT("Multiply")) ModOp = EGameplayModOp::Multiplicitive;
    else if (Op == TEXT("Divide")) ModOp = EGameplayModOp::Division;
    else if (Op == TEXT("Override")) ModOp = EGameplayModOp::Override;
    else return TEXT("ERROR: Op must be Add | Multiply | Divide | Override");
    FGameplayModifierInfo Mod;
    Mod.Attribute = FGameplayAttribute(Prop);
    Mod.ModifierOp = ModOp;
    Mod.ModifierMagnitude = FGameplayEffectModifierMagnitude(FScalableFloat(Magnitude));
    GE->Modifiers.Add(Mod);
    GE->Modify();
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return FString::Printf(TEXT("ok: %s.%s %s %g (now %d modifier(s); attribute valid=%d)"), *AttributeSetClass->GetName(), *AttributeName, *Op, Magnitude,
                           GE->Modifiers.Num(), Mod.Attribute.IsValid() ? 1 : 0);
}
