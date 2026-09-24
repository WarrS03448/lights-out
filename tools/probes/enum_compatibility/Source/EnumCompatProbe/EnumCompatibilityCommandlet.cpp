// UE 5.5: UnrealEditor-Cmd.exe EnumCompatProbe.uproject -run=EnumCompatibility -unattended -nullrhi
// Authored synthetic enums only. Does not load or launch Bodycam.
#include "EnumCompatibilityCommandlet.h"
#include "Engine/UserDefinedEnum.h"
#include "Kismet/KismetNodeHelperLibrary.h"
#include "Misc/EngineNetworkCustomVersion.h"
#include "Modules/ModuleManager.h"
#include "Serialization/BitReader.h"
#include "Serialization/BitWriter.h"
#include "Serialization/MemoryReader.h"
#include "Serialization/MemoryWriter.h"
#include "Serialization/ObjectAndNameAsStringProxyArchive.h"
#include "UObject/UnrealType.h"

IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, EnumCompatProbe, "EnumCompatProbe");

static UUserDefinedEnum* MakeEnum(const TCHAR* Name, int32 Layout) {
    auto* Result = NewObject<UUserDefinedEnum>(GetTransientPackage(), Name);
    TArray<TPair<FName, int64>> Entries;
    for (int32 Value = 0; Value < 13; ++Value) {
        const FString Short = FString::Printf(TEXT("Stock%d"), Value);
        Entries.Emplace(FName(*(FString(Name) + TEXT("::") + Short)), Value);
        Result->DisplayNameMap.Add(FName(*Short), FText::FromString(Short));
    }
    if (Layout) {
        for (const auto& Entry : TArray<TPair<FString, int64>>{{TEXT("CTF"), 13}, {TEXT("BB5"), 14}, {TEXT("BB1"), Layout == 1 ? 16 : 15}}) {
            Entries.Emplace(FName(*(FString(Name) + TEXT("::") + Entry.Key)), Entry.Value);
            Result->DisplayNameMap.Add(FName(*Entry.Key), FText::FromString(Entry.Key));
        }
    }
    Entries.Emplace(FName(*(FString(Name) + TEXT("::GameMode_MAX"))), Layout == 0 ? 13 : Layout == 1 ? 17 : 15);
    // Cooked runtime loads the Names array directly. Bypass the editor helper
    // which adds a new MAX and renumbers user enums for asset editing.
    Result->UEnum::SetEnums(Entries, UEnum::ECppForm::Namespaced, EEnumFlags::None, false);
    Result->CppType = TEXT("GameMode");
    return Result;
}

static bool PacketRoundTrip(UEnum* Sender, UEnum* Receiver, uint8 Value, uint32 NetVersion) {
    FByteProperty SendProperty(Sender, TEXT("GameMode"), RF_Transient);
    FByteProperty ReceiveProperty(Receiver, TEXT("GameMode"), RF_Transient);
    SendProperty.Enum = Sender;
    ReceiveProperty.Enum = Receiver;
    uint8 Marker = 0xA7;
    FBitWriter Writer(64, true);
    Writer.SetEngineNetVer(NetVersion);
    SendProperty.NetSerializeItem(Writer, nullptr, &Value, nullptr);
    Writer.SerializeBits(&Marker, 8);
    FBitReader Reader(Writer.GetData(), Writer.GetNumBits());
    Reader.SetEngineNetVer(NetVersion);
    uint8 Received = 0, ReceivedMarker = 0;
    ReceiveProperty.NetSerializeItem(Reader, nullptr, &Received, nullptr);
    Reader.SerializeBits(&ReceivedMarker, 8);
    return !Reader.IsError() && Received == Value && ReceivedMarker == Marker && Reader.GetPosBits() == Writer.GetNumBits();
}

int32 UEnumCompatibilityCommandlet::Main(const FString& Params) {
    auto* Stock = MakeEnum(TEXT("ProbeStock"), 0);
    auto* Old = MakeEnum(TEXT("ProbeOld"), 1);
    auto* Fixed = MakeEnum(TEXT("ProbeFixed"), 2);
    int32 Checks = 0, Failures = 0;
    auto Expect = [&](bool Pass, const TCHAR* Label) {
        ++Checks;
        if (!Pass) { ++Failures; UE_LOG(LogTemp, Error, TEXT("ENUM_PROBE FAIL: %s"), Label); }
    };
    FByteProperty Property(Fixed, TEXT("GameMode"), RF_Transient);
    Property.Enum = Fixed;
    Expect(Property.GetMaxNetSerializeBits() == 4, TEXT("fixed network width"));
    Property.Enum = Old;
    Expect(Property.GetMaxNetSerializeBits() == 5, TEXT("old negative control width"));
    for (uint32 NetVersion : {uint32(FEngineNetworkCustomVersion::EnumSerializationCompat - 1), uint32(FEngineNetworkCustomVersion::LatestVersion)}) {
        Expect(!PacketRoundTrip(Old, Stock, 3, NetVersion), TEXT("old pack reproduces packet disagreement"));
        for (uint8 Value = 0; Value <= 12; ++Value) {
            Expect(PacketRoundTrip(Fixed, Stock, Value, NetVersion), TEXT("fixed client to stock peer"));
            Expect(PacketRoundTrip(Stock, Fixed, Value, NetVersion), TEXT("stock peer to fixed client"));
        }
        for (uint8 Value = 13; Value <= 15; ++Value) {
            Expect(PacketRoundTrip(Fixed, Fixed, Value, NetVersion), TEXT("all custom mode IDs round trip"));
        }
    }
    for (uint8 Value = 13; Value <= 15; ++Value) {
        const FString Expected = Value == 13 ? TEXT("CTF") : Value == 14 ? TEXT("BB5") : TEXT("BB1");
        Expect(UKismetNodeHelperLibrary::GetValidValue(Fixed, Value) == Value, TEXT("safe enum conversion"));
        Expect(UKismetNodeHelperLibrary::GetEnumeratorUserFriendlyName(Fixed, Value) == Expected, TEXT("lobby friendly-name lookup"));
        Expect(Fixed->GetNameStringByValue(Value) == Expected, TEXT("first matching name before MAX"));
    }
    // Save-game enum property serialization uses names, independently of wire bits.
    Property.Enum = Fixed;
    uint8 Original = 15, Loaded = 0;
    TArray<uint8> Bytes;
    FMemoryWriter MemoryWriter(Bytes);
    FObjectAndNameAsStringProxyArchive SaveArchive(MemoryWriter, false);
    FStructuredArchiveFromArchive Save(SaveArchive);
    Property.SerializeItem(Save.GetSlot(), &Original, nullptr);
    FMemoryReader MemoryReader(Bytes);
    FObjectAndNameAsStringProxyArchive LoadArchive(MemoryReader, false);
    FStructuredArchiveFromArchive Load(LoadArchive);
    Property.SerializeItem(Load.GetSlot(), &Loaded, nullptr);
    Expect(Loaded == Original, TEXT("BB1 named-property save/load"));
    UE_LOG(LogTemp, Display, TEXT("ENUM_PROBE RESULT: %d checks, %d failures"), Checks, Failures);
    return Failures ? 1 : 0;
}
